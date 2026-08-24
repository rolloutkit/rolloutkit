from preflightkit.config.models import (
    Config,
    Contracts,
    InflightContract,
    InflightRequest,
    Target,
)
from preflightkit.engine.context import RunReport
from preflightkit.evidence.model import RunOutcome, Session
from preflightkit.evidence.redact import MASK, Redactor, names_a_secret
from preflightkit.probes.http import ProbeResult
from preflightkit.reporters import json_out
from preflightkit.traffic.baseline import ReadinessBaseline


def test_masks_secrets_in_nested_structures() -> None:
    redactor = Redactor(["s3cr3t-token-value", "postgresql://app:hunter2@db/app"])
    payload = {
        "url": "connecting to postgresql://app:hunter2@db/app now",
        "headers": [{"Authorization": "Bearer s3cr3t-token-value"}],
    }
    result = redactor.apply(payload)
    assert "hunter2" not in str(result)
    assert "s3cr3t" not in str(result)
    assert MASK in result["headers"][0]["Authorization"]


def test_leaves_short_values_alone() -> None:
    """Masking 'app' would corrupt every report it appears in."""
    redactor = Redactor(["app", "db"])
    assert redactor.text("the app talks to db") == "the app talks to db"


def test_only_secret_looking_names_are_masked() -> None:
    """The rule that cost a diagnosis.

    A container died with `Cannot connect to host ***redacted***` because the
    storage endpoint was an env value and every env value was masked. The one
    fact needed to explain the crash was the fact removed.
    """
    config = Config(
        target=Target(
            image="example:latest",
            port=8000,
            env={
                "MINIO_ENDPOINT": "http://host.docker.internal:9000",
                "POSTGRES_USER": "postgres",
                "MINIO_SECRET_KEY": "s3cr3t-value-here",
                "DB_PASSWORD": "hunter2-and-more",
                "API_TOKEN": "tok-abcdefgh",
            },
        )
    )
    redactor = Redactor(config.secret_values())
    text = redactor.text(
        "connect http://host.docker.internal:9000 as postgres with "
        "s3cr3t-value-here / hunter2-and-more / tok-abcdefgh"
    )
    assert "host.docker.internal:9000" in text
    assert "postgres" in text
    for secret in ("s3cr3t-value-here", "hunter2-and-more", "tok-abcdefgh"):
        assert secret not in text


def test_header_values_are_always_masked() -> None:
    """`Authorization` matches none of the four words and is still a credential."""
    config = Config(
        target=Target(image="example:latest", port=8000),
        contracts=Contracts(
            inflight=InflightContract(
                request=InflightRequest(
                    path="/slow", headers={"Authorization": "Bearer abcdefghij"}
                )
            )
        ),
    )
    assert "Bearer abcdefghij" in config.secret_values()


def test_names_a_secret_is_case_and_position_insensitive() -> None:
    for name in ("API_KEY", "apikey", "AWS_SECRET_ACCESS_KEY", "db_password", "Token"):
        assert names_a_secret(name)
    for name in ("MINIO_ENDPOINT", "POSTGRES_USER", "DATABASE_URL", "PORT"):
        assert not names_a_secret(name)


def test_readiness_baseline_is_redacted_at_the_top_level() -> None:
    secret = "token-value-from-readiness"
    config = Config(
        target=Target(
            image="example:latest",
            port=8000,
            env={"API_TOKEN": secret},
        )
    )
    report = RunReport(config=config)
    report.readiness_baseline = ReadinessBaseline(
        samples=[
            ProbeResult(
                ok=True,
                status=200,
                latency_ns=1_000_000,
                headers={"x-debug-token": secret},
                body_head=f'{{"token": "{secret}"}}',
                body_head_bytes=40,
            )
        ]
    )
    session = Session(
        run_id="pfk_test",
        image="example:latest",
        runs=[RunOutcome(report=report, results=[])],
    )

    document = json_out.build(session, "test")

    assert secret not in str(document["readiness_baseline"])
    assert MASK in str(document["readiness_baseline"])
