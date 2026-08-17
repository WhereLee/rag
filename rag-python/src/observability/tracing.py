"""
OTEL 全链路追踪：本地 Zipkin（复用短链项目设施，9411）。

span 命名约定：rag.<阶段>（parse/retrieve/rerank/generate/route/reflect/eval）
"""
import logging
import os

logger = logging.getLogger("rag.tracing")

_tracer = None


def _zipkin_reachable(endpoint: str) -> bool:
    """Zipkin 端点可达性探测（1s 超时）。不可达时降级 no-op tracing，
    避免 exporter 反复重试把异常刷满日志（实测：Zipkin 未启动时每 5s 刷一次 traceback）。"""
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(endpoint)
        host = u.hostname or "localhost"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def setup_tracing(service_name: str = "rag-doc-qa",
                  zipkin_endpoint: str = ""):
    """初始化 TracerProvider + Zipkin exporter（失败不阻塞业务）。"""
    global _tracer
    if _tracer is not None:
        return _tracer
    endpoint = zipkin_endpoint or os.getenv(
        "ZIPKIN_ENDPOINT", "http://localhost:9411/api/v2/spans")
    try:
        from opentelemetry import trace
        if not _zipkin_reachable(endpoint):
            # 无 agent 时 get_tracer 返回 ProxyTracerProvider 的 no-op tracer
            logger.info("Zipkin 不可达 (%s)，tracing 降级 no-op", endpoint)
            _tracer = trace.get_tracer(service_name)
            return _tracer
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.zipkin.json import ZipkinExporter

        provider = TracerProvider(resource=Resource.create(
            {"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(
            ZipkinExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(service_name)
        logger.info("OTEL tracing -> %s", endpoint)
    except Exception as e:
        logger.warning("tracing setup failed (no-op): %s", e)
        from opentelemetry import trace
        _tracer = trace.get_tracer(service_name)
    return _tracer


def get_tracer():
    global _tracer
    if _tracer is None:
        return setup_tracing()
    return _tracer


def span(name: str, **attrs):
    """便捷上下文管理器：with span("rag.retrieve", top_k=8): ..."""
    return get_tracer().start_as_current_span(name, attributes=attrs)
