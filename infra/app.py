#!/usr/bin/env python3
"""CDK app entry point.

Representative slice of the onboarding pipeline: the ingestion trigger, the storage
buckets, and the compute that runs the Part 1 detector. It synthesises with no AWS
credentials and no context.

One stack definition, promoted through environments by context:

    cdk synth                       # dev  (default): tears down cleanly
    cdk synth -c stage=staging      # retains data + logs, wires alarms
    cdk synth -c stage=prod         # same, prod naming

`-c account=... -c region=...` pins the deploy target; without them the stack is
environment-agnostic (fine for synth).
"""

from __future__ import annotations

import aws_cdk as cdk
from stacks.ingestion_stack import IngestionStack

app = cdk.App()

stage_context = app.node.try_get_context("stage")
stage = "dev" if stage_context is None else str(stage_context)
account = app.node.try_get_context("account")
region = app.node.try_get_context("region")
env = cdk.Environment(account=account, region=region) if account or region else None

IngestionStack(
    app,
    f"LoanTapeIngestion-{stage}",
    stage=stage,
    env=env,
    description=f"Loan-tape ingestion and data-quality detection ({stage})",
)

cdk.Tags.of(app).add("project", "loan-tape-dq")
cdk.Tags.of(app).add("stage", stage)
cdk.Tags.of(app).add("owner", "platform")

app.synth()
