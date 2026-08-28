"""JUnit XML report: one testcase for each aggregated contract."""

from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from rolloutkit.contracts.base import Status
from rolloutkit.evidence.model import Session
from rolloutkit.evidence.redact import Redactor

_EVIDENCE_LIMIT = 2_000


def dump(session: Session, version: str) -> str:
    config = session.runs[-1].report.config
    redactor = Redactor(config.secret_values())
    results = session.aggregated
    skipped = sum(r.status in (Status.SKIP, Status.INCONCLUSIVE) for r in results)
    failures = sum(r.status in (Status.FAIL, Status.ERROR) for r in results)
    suite = ET.Element(
        "testsuite",
        {
            "name": "rolloutkit",
            "tests": str(len(results)),
            "failures": str(failures),
            "errors": "0",
            "skipped": str(skipped),
            "time": f"{sum(run.duration_ms for run in session.runs) / 1000:.3f}",
        },
    )
    suite.set("hostname", session.image)
    properties = ET.SubElement(suite, "properties")
    ET.SubElement(properties, "property", {"name": "tool.version", "value": version})
    for result in results:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "rolloutkit.contracts",
                "name": f"{result.id} {result.name}",
            },
        )
        summary = redactor.text(result.summary)
        if result.status in (Status.SKIP, Status.INCONCLUSIVE):
            ET.SubElement(
                case,
                "skipped",
                {"message": f"{result.status}: {summary}"},
            ).text = summary
        elif result.status in (Status.FAIL, Status.ERROR):
            evidence = json.dumps(
                redactor.apply(result.evidence), ensure_ascii=False, sort_keys=True
            )
            ET.SubElement(
                case,
                "failure",
                {"message": summary, "type": str(result.status)},
            ).text = evidence[:_EVIDENCE_LIMIT]
        elif result.status is not Status.PASS:
            ET.SubElement(case, "system-out").text = f"{result.status}: {summary}"
    return ET.tostring(suite, encoding="unicode", xml_declaration=True)
