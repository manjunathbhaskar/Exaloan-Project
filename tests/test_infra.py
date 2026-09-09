from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def cdk_modules():
    cdk = pytest.importorskip("aws_cdk", reason="install the infra extra for CDK assertions")
    assertions = importlib.import_module("aws_cdk.assertions")
    stack_type = importlib.import_module("infra.stacks.ingestion_stack").IngestionStack
    return cdk, assertions.Template, stack_type


def resources_of(template, kind):
    return [r for r in template["Resources"].values() if r["Type"] == kind]


@pytest.mark.parametrize("stage", ["dev", "staging", "prod"])
def test_storage_permissions_retries_retention_and_asset(cdk_modules, tmp_path, stage):
    cdk, template_type, stack_type = cdk_modules
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stack = stack_type(app, f"LoanTapeIngestion-{stage}", stage=stage)
    template = template_type.from_stack(stack)
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "BillingMode": "PAY_PER_REQUEST",
            "KeySchema": [{"AttributeName": "run_id", "KeyType": "HASH"}],
            "SSESpecification": {"SSEEnabled": True, "SSEType": "KMS"},
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
        },
    )
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"PackageType": "Image", "Architectures": ["arm64"], "Timeout": 600},
    )
    template.has_resource_properties(
        "AWS::Lambda::EventInvokeConfig",
        {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 21600},
    )
    data = template.to_json()
    [detector] = [
        r
        for r in resources_of(data, "AWS::Lambda::Function")
        if r["Properties"].get("PackageType") == "Image"
    ]
    environment = detector["Properties"]["Environment"]["Variables"]
    assert {"RAW_BUCKET", "RESULTS_BUCKET", "RUN_TABLE"} <= environment.keys()
    assert environment["STAGE"] == stage
    assert int(environment["LEASE_SECONDS"]) > detector["Properties"]["Timeout"]
    assert int(environment["MAX_RUNTIME_SECONDS"]) == detector["Properties"]["Timeout"]
    assert 0 < int(environment["MAX_INPUT_BYTES"]) <= 50 * 1024 * 1024
    expected_policy = "Delete" if stage == "dev" else "Retain"
    for kind in (
        "AWS::S3::Bucket",
        "AWS::DynamoDB::Table",
        "AWS::SQS::Queue",
        "AWS::Logs::LogGroup",
        "AWS::KMS::Key",
    ):
        for resource in resources_of(data, kind):
            assert resource["DeletionPolicy"] == expected_policy
            assert resource["UpdateReplacePolicy"] == expected_policy
    [logs] = resources_of(data, "AWS::Logs::LogGroup")
    assert logs["Properties"]["RetentionInDays"] == (30 if stage == "dev" else 365)
    assert "KmsKeyId" in logs["Properties"]
    [queue] = resources_of(data, "AWS::SQS::Queue")
    assert "RedrivePolicy" not in queue["Properties"]
    assert "KmsMasterKeyId" in queue["Properties"]
    [rule] = resources_of(data, "AWS::Events::Rule")
    assert rule["Properties"]["Name"] == f"loan-tape-dq-{stage}-uploads"
    [target] = rule["Properties"]["Targets"]
    assert target["RetryPolicy"] == {"MaximumEventAgeInSeconds": 7200, "MaximumRetryAttempts": 2}
    assert "DeadLetterConfig" in target and "DeadLetterConfig" in detector["Properties"]
    [key] = resources_of(data, "AWS::KMS::Key")
    statements = key["Properties"]["KeyPolicy"]["Statement"]
    for principal in ("events.amazonaws.com", "cloudwatch.amazonaws.com"):
        [grant] = [s for s in statements if s.get("Principal", {}).get("Service") == principal]
        assert {"kms:Decrypt", "kms:GenerateDataKey*"} <= set(grant["Action"])
        assert "aws:SourceAccount" in grant["Condition"]["StringEquals"]
        assert "aws:SourceArn" in grant["Condition"]["ArnLike"]
        assert stage in json.dumps(grant["Condition"]["ArnLike"])
    assert any("kms:EncryptionContext:aws:logs:arn" in json.dumps(s) for s in statements)
    [breaker] = [
        a
        for a in resources_of(data, "AWS::CloudWatch::Alarm")
        if a["Properties"].get("MetricName") == "CircuitBreakerTripped"
    ]
    assert breaker["Properties"]["Dimensions"] == [{"Name": "Stage", "Value": stage}]
    policies = json.dumps(resources_of(data, "AWS::IAM::Policy"))
    assert "s3:GetObjectVersion" in policies
    assert "diagnostics/runs/*" in policies
    assert "dynamodb:PutItem" in policies and "dynamodb:UpdateItem" in policies
    assembly = Path(app.synth().directory)
    assets = json.loads((assembly / f"LoanTapeIngestion-{stage}.assets.json").read_text())
    [(asset_id, asset)] = assets["dockerImages"].items()
    assert asset["source"]["platform"] == "linux/arm64"
    assert asset_id in json.dumps(detector["Properties"]["Code"])
    context = assembly / asset["source"]["directory"]
    assert (context / "infra/lambda/Dockerfile").is_file()
    assert (context / "infra/lambda/smoke.py").is_file()
    assert (context / "requirements-runtime.lock").is_file()
    assert (context / "src/loan_dq/engine.py").is_file()
    assert not (context / "data").exists()
    assert not (context / "infra/.venv").exists()
    assert not (context / "infra/cdk.out").exists()


def test_unknown_stage_fails_closed(cdk_modules, tmp_path):
    cdk, _, stack_type = cdk_modules
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    with pytest.raises(ValueError, match="stage must"):
        stack_type(app, "Typo", stage="production")


def test_ci_verifies_staged_asset_without_publishing():
    root = Path(__file__).resolve().parents[1]
    pipeline = yaml.safe_load((root / "bitbucket-pipelines.yml").read_text())
    pr_steps = pipeline["pipelines"]["pull-requests"]["**"]
    main_steps = pipeline["pipelines"]["branches"]["main"]

    # Pull requests never publish: quality + verify-asset only.
    assert len(pr_steps) == 2
    # Main adds one publish step; it must be manual so nothing is published automatically.
    assert len(main_steps) == 3
    assert main_steps[2]["step"].get("trigger") == "manual"

    for steps in (pr_steps, main_steps[:2]):
        quality, asset = (entry["step"] for entry in steps)
        assert any("import aws_cdk" in line for line in quality["script"])
        assert asset["runtime"]["cloud"]["arch"] == "arm"
        script = "\n".join(asset["script"])
        assert "docker push" not in script and "aws sts" not in script
        assert "oidc" not in asset
        [verification] = [line for line in asset["script"] if line.startswith("python - <<'PY'")]
        code = verification.split("\n", 1)[1].rsplit("\nPY", 1)[0]
        ast.parse(code)
        assert 'context = assembly / source["directory"]' in code
        assert 'set(prod_assets["dockerImages"]) == {asset_id}' in code
        assert 'image["Id"]' in code and '"--network", "none"' in code
        assert '"published": False' in code and "verified-image.json" in code


def test_ci_publish_step_is_oidc_federated_with_no_static_keys():
    root = Path(__file__).resolve().parents[1]
    text = (root / "bitbucket-pipelines.yml").read_text()
    pipeline = yaml.safe_load(text)
    publish = pipeline["pipelines"]["branches"]["main"][2]["step"]
    script = "\n".join(publish["script"])

    assert publish["oidc"] is True
    assert publish.get("trigger") == "manual"
    # Federation, not stored credentials.
    assert "aws sts assume-role-with-web-identity" in script
    assert "$BITBUCKET_STEP_OIDC_TOKEN" in script
    assert "$AWS_OIDC_ROLE_ARN" in script
    # Pushes the verified asset by id; never a floating tag, never a deploy.
    assert "docker push" in script and ":latest" not in script
    assert "cdk deploy" not in text
    # No long-lived key literal appears anywhere in the pipeline file.
    assert "AKIA" not in text
