import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from flat_agent.llm import ModelError, OllamaRouter, parse_object
from flat_agent.models import ReplyOutput


def test_real_chatollama_serializes_cloud_request_without_format():
    import json
    import httpx
    from langchain_ollama import ChatOllama
    seen=[]
    def handle(request):
        payload=json.loads(request.content)
        seen.append(payload)
        assert str(request.url)=="https://ollama.com/api/chat"
        assert payload.get("format") is None
        return httpx.Response(200,json={"model":"glm-5.3-flash","created_at":"2026-10-18T00:00:00Z","message":{"role":"assistant","content":"{\"intent\":\"other\",\"summary\":\"ok\"}","thinking":"not persisted"},"done":True})
    model=ChatOllama(model="glm-5.3-flash",base_url="https://ollama.com",reasoning=True,client_kwargs={"transport":httpx.MockTransport(handle)})
    result=model.invoke("Return JSON",stream=False)
    assert parse_object(result.content,ReplyOutput).summary=="ok"
    assert len(seen)==1


def test_parser_rejects_extra_claim_fields_and_nonjson():
    with pytest.raises(ValueError):
        parse_object('{"intent":"other","summary":"ok","secret":"unexpected"}',ReplyOutput)
    with pytest.raises(ValueError):
        parse_object('Some prose {"intent":"other","summary":"ok"}',ReplyOutput)
    assert parse_object('```json\n{"intent":"other","summary":"ok"}\n```',ReplyOutput).intent=="other"


def test_cloud_adapter_is_langchain_and_repairs_are_bounded(settings,store,monkeypatch):
    settings.save_secret("OLLAMA_API_KEY","test-key")
    store.set_setting("catalog",[{"name":"glm-5.3","capabilities":["completion"]}])
    calls=[]
    def factory(**kwargs):
        assert kwargs["base_url"]=="https://ollama.com"
        assert kwargs["client_kwargs"]["headers"]["Authorization"]=="Bearer test-key"
        assert kwargs.get("format") is None
        calls.append(kwargs["model"])
        return RunnableLambda(lambda _:AIMessage(content="not JSON"))
    monkeypatch.setattr("flat_agent.llm.ChatOllama",factory)
    with pytest.raises(ModelError,match="validation"):
        OllamaRouter(settings,store).ask("interpret_complex_reply",ReplyOutput,"Classify",{})
    assert len(calls)==2
    assert store.model_stats()[0]["calls"]==2


def test_text_only_model_cannot_receive_screenshot(settings,store):
    settings.save_secret("OLLAMA_API_KEY","test-key")
    roles=store.setting("roles")
    roles["interpret_screen"]["model"]="glm-5.3"
    store.set_setting("roles",roles)
    store.set_setting("catalog",[{"name":"glm-5.3","capabilities":["completion","tools"]}])
    with pytest.raises(ModelError,match="capabilities"):
        OllamaRouter(settings,store).ask("interpret_screen",ReplyOutput,"Inspect",{},image=b"image")
    assert store.model_stats()==[]
