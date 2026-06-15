"""Extract event handlers (OnError, OnWarning, etc.) from DTSX XML."""

from __future__ import annotations

from lxml import etree

from ssis_migration.cir.models import EventHandler
from ssis_migration.parser.ns import (
    ATTR_EVENT_NAME,
    DTS,
    DTS_EVENT_HANDLER,
    DTS_EVENT_HANDLERS,
)
from .control_flow import ControlFlowExtractor

ATTR_EVENT_NAME_LOCAL = f"{{{DTS}}}EventName"


class EventHandlerExtractor:
    def __init__(self, root: etree._Element) -> None:
        self._root = root

    def extract(self) -> list[EventHandler]:
        results: list[EventHandler] = []
        handlers_el = self._root.find(DTS_EVENT_HANDLERS)
        if handlers_el is None:
            return results

        for eh_el in handlers_el.findall(DTS_EVENT_HANDLER):
            event_name = eh_el.get(ATTR_EVENT_NAME_LOCAL, "OnError")
            # Reuse ControlFlowExtractor to parse executables inside the handler
            cf_ext = ControlFlowExtractor(eh_el)
            cf = cf_ext.extract()
            results.append(EventHandler(
                event=event_name,
                scope="package",
                executables=cf.execution_tree,
            ))
        return results
