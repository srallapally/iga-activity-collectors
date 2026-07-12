# examples/racf_audit_log_collector.py
"""
STUB — not yet implemented.

RACF Audit Log Collector: Fixed-format RACF SMF audit log records. Subclasses log_file_collector with a pre-populated regex. ASSUMPTION: exact SMF layout not yet validated against real RACF output.

Will subclass iga_collectors.base.BaseCollector, implementing poll(),
next_position(), and map_to_event() for this source, and expose a
module-level create_collector(config) -> BaseCollector entry point for
discovery.
"""
