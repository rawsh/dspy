import functools
import json
import logging
from typing import Any, Literal, Optional, cast
import uuid

import backoff
import openai
from openai import OpenAI

from dsp.modules.cache_utils import CacheMemory, NotebookCacheMemory, cache_turn_on
from dsp.modules.lm import LM

try:
    OPENAI_LEGACY = int(openai.version.__version__[0]) == 0
except Exception:
    OPENAI_LEGACY = True

try:
    import openai.error
    from openai.openai_object import OpenAIObject

    ERRORS = (openai.error.RateLimitError,)
except Exception:
    ERRORS = (openai.RateLimitError,)
    OpenAIObject = dict


def backoff_hdlr(details):
    """Handler from https://pypi.org/project/backoff/"""
    print(
        "Backing off {wait:0.1f} seconds after {tries} tries "
        "calling function {target} with kwargs "
        "{kwargs}".format(**details),
    )


class GPT3(LM):
    """Wrapper around OpenAI's GPT API.

    Args:
        model (str, optional): OpenAI supported LLM model to use. Defaults to "gpt-3.5-turbo-instruct".
        api_key (Optional[str], optional): API provider Authentication token. use Defaults to None.
        api_provider (Literal["openai"], optional): The API provider to use. Defaults to "openai".
        model_type (Literal["chat", "text"], optional): The type of model that was specified. Mainly to decide the optimal prompting strategy. Defaults to "text".
        **kwargs: Additional arguments to pass to the API provider.
    """

    def __init__(
        self,
        model: str = "gpt-3.5-turbo-instruct",
        api_key: Optional[str] = None,
        api_provider: Literal["openai"] = "openai",
        api_base: Optional[str] = None,
        model_type: Literal["chat", "text"] = None,
        system_prompt: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(model)
        self.provider = "openai"

        if OPENAI_LEGACY:
            openai.api_type = api_provider

            if api_key:
                openai.api_key = api_key

            if api_base:
                openai.api_base = api_base
        else:
            self.client = OpenAI(api_key=api_key, base_url=api_base)
            self.client.api_type = api_provider

        self.system_prompt = system_prompt

        assert (
            api_provider != "azure"
        ), "Azure functionality with base OpenAI has been deprecated, please use dspy.AzureOpenAI instead."

        default_model_type = (
            "chat"
            if ("gpt-3.5" in model or "turbo" in model or "gpt-4" in model) and ("instruct" not in model)
            else "text"
        )
        self.model_type = model_type if model_type else default_model_type

        self.kwargs = {
            "temperature": 0.0,
            "max_tokens": 150,
            "top_p": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
            "n": 1,
            **kwargs,
        }  # TODO: add kwargs above for </s>

        self.kwargs["model"] = model
        self.history: list[dict[str, Any]] = []

        # cached completions client
        self.cache = CachedCompletions(self._openai_client())

    def _openai_client(self):
        if OPENAI_LEGACY:
            return openai
        else:
            return self.client

    def log_usage(self, response):
        """Log the total tokens from the OpenAI API response."""
        usage_data = response.get("usage")
        if usage_data:
            total_tokens = usage_data.get("total_tokens")
            logging.debug(f"OpenAI Response Token Usage: {total_tokens}")

    def basic_request(self, prompt: str, **kwargs):
        raw_kwargs = kwargs

        kwargs = {**self.kwargs, **kwargs}
        if self.model_type == "chat":
            # caching mechanism requires hashable kwargs
            messages = [{"role": "user", "content": prompt}]
            if self.system_prompt:
                messages.insert(0, {"role": "system", "content": self.system_prompt})
            kwargs["messages"] = messages
            kwargs = {"stringify_request": json.dumps(kwargs)}
            response = self.cache.chat_request(**kwargs)

        else:
            kwargs["prompt"] = prompt
            response = self.cache.completions_request(**kwargs)

        history = {
            "prompt": prompt,
            "response": response,
            "kwargs": kwargs,
            "raw_kwargs": raw_kwargs,
        }
        self.history.append(history)

        return response

    @backoff.on_exception(
        backoff.expo,
        ERRORS,
        max_time=1000,
        on_backoff=backoff_hdlr,
    )
    def request(self, prompt: str, **kwargs):
        """Handles retreival of GPT-3 completions whilst handling rate limiting and caching."""
        if "model_type" in kwargs:
            del kwargs["model_type"]

        return self.basic_request(prompt, **kwargs)

    def _get_choice_text(self, choice: dict[str, Any]) -> str:
        if self.model_type == "chat":
            return choice["message"]["content"]
        return choice["text"]

    def __call__(
        self,
        prompt: str,
        only_completed: bool = True,
        return_sorted: bool = False,
        **kwargs,
    ) -> list[dict[str, Any]]:
        """Retrieves completions from GPT-3.

        Args:
            prompt (str): prompt to send to GPT-3
            only_completed (bool, optional): return only completed responses and ignores completion due to length. Defaults to True.
            return_sorted (bool, optional): sort the completion choices using the returned probabilities. Defaults to False.

        Returns:
            list[dict[str, Any]]: list of completion choices
        """

        assert only_completed, "for now"
        assert return_sorted is False, "for now"

        # if kwargs.get("n", 1) > 1:
        #     if self.model_type == "chat":
        #         kwargs = {**kwargs}
        #     else:
        #         kwargs = {**kwargs, "logprobs": 5}

        response = self.request(prompt, **kwargs)

        self.log_usage(response)
        choices = response["choices"]

        completed_choices = [c for c in choices if c["finish_reason"] != "length"]

        if only_completed and len(completed_choices):
            choices = completed_choices

        if kwargs.get("logprobs", False):
            completions = [{'text': self._get_choice_text(c), 'logprobs': c["logprobs"]} for c in choices]
        else:
            completions = [self._get_choice_text(c) for c in choices]

        if return_sorted and kwargs.get("n", 1) > 1:
            scored_completions = []

            for c in choices:
                tokens, logprobs = (
                    c["logprobs"]["tokens"],
                    c["logprobs"]["token_logprobs"],
                )

                if "<|endoftext|>" in tokens:
                    index = tokens.index("<|endoftext|>") + 1
                    tokens, logprobs = tokens[:index], logprobs[:index]

                avglog = sum(logprobs) / len(logprobs)
                scored_completions.append((avglog, self._get_choice_text(c), logprobs))
            scored_completions = sorted(scored_completions, reverse=True)
            if logprobs:
                completions = [{'text': c, 'logprobs': lp} for _, c, lp in scored_completions]
            else:
                completions = [c for _, c in scored_completions]

        return completions


import functools
import weakref

def weak_lru(maxsize=128, typed=False):
    'LRU Cache decorator that keeps a weak reference to "self"'
    def wrapper(func):

        @functools.lru_cache(maxsize, typed)
        def _func(_self, *args, **kwargs):
            return func(_self(), *args, **kwargs)

        @functools.wraps(func)
        def inner(self, *args, **kwargs):
            return _func(weakref.ref(self), *args, **kwargs)

        return inner

    return wrapper

class CachedCompletions:
    def __init__(self, client: OpenAI):
        # generate uuid for cache
        self.client = client
        self.cache_gpt3_request_v2 = CacheMemory.cache(self.cached_gpt3_request_v2, ignore=['self'])
        self.cached_gpt3_request_v2_wrapped = NotebookCacheMemory.cache(self.cached_gpt3_request_v2_wrapped, ignore=['self'])
        self._cached_gpt3_turbo_request_v2 = CacheMemory.cache(self._cached_gpt3_turbo_request_v2, ignore=['self'])
        self._cached_gpt3_turbo_request_v2_wrapped = NotebookCacheMemory.cache(self._cached_gpt3_turbo_request_v2_wrapped, ignore=['self'])
        self.v1_cached_gpt3_request_v2 = CacheMemory.cache(self.v1_cached_gpt3_request_v2, ignore=['self'])
        self.v1_cached_gpt3_request_v2_wrapped = NotebookCacheMemory.cache(self.v1_cached_gpt3_request_v2_wrapped, ignore=['self'])
        self.v1_cached_gpt3_turbo_request_v2 = CacheMemory.cache(self.v1_cached_gpt3_turbo_request_v2, ignore=['self'])
        self.v1_cached_gpt3_turbo_request_v2_wrapped = NotebookCacheMemory.cache(self.v1_cached_gpt3_turbo_request_v2_wrapped, ignore=['self'])

    def cached_gpt3_request_v2(self, **kwargs):
        del kwargs["model_uuid"]
        return self.client.Completion.create(**kwargs)

    @weak_lru(maxsize=None if cache_turn_on else 0)
    def cached_gpt3_request_v2_wrapped(self, **kwargs):
        return self.cached_gpt3_request_v2(**kwargs)

    def _cached_gpt3_turbo_request_v2(self, **kwargs) -> OpenAIObject:
        if "stringify_request" in kwargs:
            kwargs = json.loads(kwargs["stringify_request"])
        return cast(OpenAIObject, openai.ChatCompletion.create(**kwargs))

    @weak_lru(maxsize=None if cache_turn_on else 0)
    def _cached_gpt3_turbo_request_v2_wrapped(self, **kwargs) -> OpenAIObject:
        return self._cached_gpt3_turbo_request_v2(**kwargs)

    def v1_cached_gpt3_request_v2(self, **kwargs):
        del kwargs["model_uuid"]
        return self.client.completions.create(**kwargs)

    @weak_lru(maxsize=None if cache_turn_on else 0)
    def v1_cached_gpt3_request_v2_wrapped(self, **kwargs):
        return self.v1_cached_gpt3_request_v2(**kwargs)

    def v1_cached_gpt3_turbo_request_v2(self, **kwargs):
        if "stringify_request" in kwargs:
            kwargs = json.loads(kwargs["stringify_request"])
        return self.client.chat.completions.create(**kwargs)

    @weak_lru(maxsize=None if cache_turn_on else 0)
    def v1_cached_gpt3_turbo_request_v2_wrapped(self, **kwargs):
        return self.v1_cached_gpt3_turbo_request_v2(**kwargs)

    def chat_request(self, **kwargs):
        if OPENAI_LEGACY:
            return self._cached_gpt3_turbo_request_v2_wrapped(**kwargs)

        return self.v1_cached_gpt3_turbo_request_v2_wrapped(**kwargs).model_dump()

    def completions_request(self, **kwargs):
        if OPENAI_LEGACY:
            return self.cached_gpt3_request_v2_wrapped(**kwargs)

        return self.v1_cached_gpt3_request_v2_wrapped(**kwargs).model_dump()