# examples/otel_collector.py
"""
OpenTelemetry Span Collector — consumes spans from a Kafka topic that an
OpenTelemetry Collector's kafkaexporter has published to.

Why Kafka instead of tailing a file: span volume is typically orders of
magnitude higher than admin/audit log volume. The OTel Collector's
`filter` processor can drop non-identity-bearing spans upstream, before
they ever reach Kafka.

Kafka producer-side config assumed (set in the OTel Collector, not here):
    exporters:
      kafka:
        brokers: [kafka.example.com:9092]
        traces: {topic: otel-traces, encoding: otlp_json}
`encoding: otlp_json` matters — the exporter's default is `otlp_proto`
(binary Protobuf), NOT decoded here.

IMPORTANT delivery-guarantee gap, stated plainly: relies on
confluent-kafka's default auto-commit, independent of whether
ActivityUploader has actually uploaded those spans successfully. A
production version needs manual offset commits gated on confirmed
upload success — not implemented here.

CRITICAL DIFFERENCE from every other declaratively-mapped collector in
this project: filter_by_checkpoint=False is passed explicitly below.
Kafka's own committed consumer offset (per group_id) drives resumption
here, not the timestamp checkpoint DeclarativeMappedCollector otherwise
centralizes for every REST-polled source. Leaving the default (True)
would be a real bug: distributed tracing has legitimate clock skew across
services, so a span's timestamp can land at or before the checkpoint
without being a duplicate, and the centralized filter would silently drop
valid spans. The base CheckpointStore is still updated (for consistency
with every other collector's interface, and as an audit trail of "last
event time processed"), it just doesn't drive filtering here.

Field mapping is declarative — see otel_collector.fieldmap.json.
Structural reshaping (resourceSpans/scopeSpans/spans traversal, OTLP's
attribute-list-to-dict conversion) stays in poll_records(), same
category as Google Workspace's multi-event flattening — it's reshaping
the wire format into a usable flat record, not a semantic mapping
decision.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from iga_collectors.base import CheckpointStore, PassthroughCorrelator
from iga_collectors.field_mapping import DeclarativeMappedCollector

FIELD_MAP_PATH = Path(__file__).parent / "otel_collector.fieldmap.json"


def _attr_value_to_str(value_obj: Any) -> Optional[str]:
    if not isinstance(value_obj, dict):
        return None
    for key in ("stringValue", "intValue", "boolValue", "doubleValue"):
        if key in value_obj:
            return str(value_obj[key])
    return None


def _attrs_to_dict(attrs_list: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for attr in attrs_list:
        key = attr.get("key")
        value = _attr_value_to_str(attr.get("value", {}))
        if key and value is not None:
            result[key] = value
    return result


class OTelKafkaSpanCollector(DeclarativeMappedCollector):
    def __init__(
        self,
        *,
        bootstrap_servers: str,
        topic: str,
        group_id: str,
        identity_attribute: str = "enduser.id",
        target_attribute: Optional[str] = None,
        poll_timeout_seconds: float = 5.0,
        max_messages_per_run: int = 10_000,
        consumer_factory: Optional[Callable[[str, str, str], Any]] = None,
        **declarative_kwargs: Any,
    ):
        super().__init__(**declarative_kwargs)
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._group_id = group_id
        self._identity_attribute = identity_attribute
        self._target_attribute = target_attribute
        self._poll_timeout_seconds = poll_timeout_seconds
        self._max_messages_per_run = max_messages_per_run
        self._consumer_factory = consumer_factory or _default_consumer_factory

    def poll_records(self, since_position: Optional[str]) -> Iterator[dict[str, Any]]:
        consumer = self._consumer_factory(self._bootstrap_servers, self._group_id, self._topic)
        try:
            consumed = 0
            while consumed < self._max_messages_per_run:
                msg = consumer.poll(timeout=self._poll_timeout_seconds)
                if msg is None:
                    break
                if msg.error():
                    raise RuntimeError(f"Kafka consumer error: {msg.error()}")
                batch = json.loads(msg.value())
                yield from self._iter_span_records(batch)
                consumed += 1
        finally:
            consumer.close()

    def _iter_span_records(self, batch: dict[str, Any]) -> Iterator[dict[str, Any]]:
        for resource_spans in batch.get("resourceSpans", []):
            resource_attrs = _attrs_to_dict(
                resource_spans.get("resource", {}).get("attributes", [])
            )
            service_name = resource_attrs.get("service.name", "unknown_service")

            for scope_spans in resource_spans.get("scopeSpans", []):
                for span in scope_spans.get("spans", []):
                    span_attrs = _attrs_to_dict(span.get("attributes", []))

                    user = span_attrs.get(self._identity_attribute)
                    if not user:
                        continue

                    if not span.get("endTimeUnixNano"):
                        continue

                    target = (
                        span_attrs.get(self._target_attribute)
                        if self._target_attribute else None
                    ) or service_name

                    yield {
                        "user": user,
                        "action": span.get("name", "unknown_operation"),
                        "target": target,
                        "endTimeUnixNano": span.get("endTimeUnixNano"),
                        "statusCode": span.get("status", {}).get("code"),
                    }


def _default_consumer_factory(bootstrap_servers: str, group_id: str, topic: str):
    from confluent_kafka import Consumer  # confluent-kafka
    consumer = Consumer({
        "bootstrap.servers": bootstrap_servers,
        "group.id": group_id,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    })
    consumer.subscribe([topic])
    return consumer


# ---------------------------------------------------------------------------
# Reference example.
# ---------------------------------------------------------------------------

def create_collector(config: dict[str, Any]):
    field_map = json.loads(FIELD_MAP_PATH.read_text())

    return OTelKafkaSpanCollector(
        bootstrap_servers=config["kafka_bootstrap_servers"],
        topic=config.get("otel_kafka_topic", "otel-traces"),
        group_id=config.get("otel_kafka_group_id", "iga-nhi-collector"),
        field_map=field_map,
        source_timezone=timezone.utc,
        filter_by_checkpoint=False,
        collector_id="otel_spans",
        source_system="opentelemetry",
        correlator=PassthroughCorrelator(),
        checkpoint_store=CheckpointStore(Path(config["checkpoint_path"])),
    )