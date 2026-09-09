"""Ingestion + detection slice.

Flow: a lender upload lands in the raw S3 bucket; an EventBridge "Object Created" rule
invokes the detector Lambda; the Lambda writes the report to the results bucket, emits
a circuit-breaker metric, and routes failed events to an SQS dead-letter queue.

Only what the brief asks for: the ingestion trigger, storage (raw is an immutable audit
copy, results is queryable), and one compute resource that runs the packaged Part 1
code, all encrypted with one customer-managed KMS key. RDS/Aurora serving, a Step
Functions fan-out to Fargate for large tapes, and VPC placement are described in
docs/architecture.md, not built here.
"""

from __future__ import annotations

import pathlib

from aws_cdk import CfnOutput, Duration, IgnoreMode, RemovalPolicy, Size, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
TAPE_SUFFIXES = (".csv", ".parquet", ".jsonl", ".xlsx")


class IngestionStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str = "dev",
        **kwargs: object,
    ) -> None:
        if stage not in {"dev", "staging", "prod"}:
            raise ValueError("stage must be dev, staging or prod")
        super().__init__(scope, construct_id, **kwargs)

        # One stack definition, three environments. `durable` = staging/prod: keep
        # data and logs on stack delete, keep logs a year. dev tears everything down
        # so a throwaway stack leaves nothing behind.
        durable = stage in ("staging", "prod")
        retain = RemovalPolicy.RETAIN if durable else RemovalPolicy.DESTROY
        log_retention = logs.RetentionDays.ONE_YEAR if durable else logs.RetentionDays.ONE_MONTH

        # --- encryption -----------------------------------------------------
        # One customer-managed KMS key for every store that can hold PII or a
        # reference to it (both buckets, the DLQ, the alert topic). Rotated yearly;
        # retained in staging/prod so encrypted objects stay readable.
        data_key = kms.Key(
            self,
            "DataKey",
            description="loan-tape-dq: raw tapes, results, DLQ, alerts",
            enable_key_rotation=True,
            removal_policy=retain,
        )

        # --- storage ---------------------------------------------------------
        # Raw tapes: immutable audit copy. Versioned + retained (staging/prod) so a
        # rule change can reprocess history; lifecycle to Glacier keeps that cheap.
        # bucket_key_enabled makes S3 derive per-object keys locally from one
        # short-lived data key, so KMS request volume stays flat as tapes arrive.
        raw_tapes = s3.Bucket(
            self,
            "RawTapes",
            versioned=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=data_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            event_bridge_enabled=True,
            removal_policy=retain,
            auto_delete_objects=not durable,
            lifecycle_rules=[
                s3.LifecycleRule(
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(90),
                        )
                    ],
                    noncurrent_version_expiration=Duration.days(365),
                )
            ],
        )

        results = s3.Bucket(
            self,
            "Results",
            versioned=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=data_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=retain,
            auto_delete_objects=not durable,
        )

        runs = dynamodb.Table(
            self,
            "Runs",
            partition_key=dynamodb.Attribute(name="run_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=data_key,
            point_in_time_recovery=True,
            removal_policy=retain,
        )

        # --- failure handling ----------------------------------------------
        dead_letter = sqs.Queue(
            self,
            "IngestionDeadLetter",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=data_key,
            removal_policy=retain,
        )
        alerts = sns.Topic(
            self,
            "Alerts",
            display_name="loan-tape-dq alerts",
            master_key=data_key,
        )

        # --- compute: the Part 1 detector, packaged as a container image ----
        # loan_dq needs pandas / openpyxl / pydantic, so it ships as an image rather
        # than a zip. The same image runs on Fargate for large tapes.
        detector_logs = logs.LogGroup(
            self,
            "DetectorLogs",
            log_group_name=f"/loan-tape-dq/{stage}/detector",
            retention=log_retention,
            removal_policy=retain,
            encryption_key=data_key,
        )
        data_key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal(f"logs.{self.region}.{self.url_suffix}")],
                actions=[
                    "kms:Encrypt*",
                    "kms:Decrypt*",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:DescribeKey",
                ],
                resources=["*"],
                conditions={
                    "ArnEquals": {
                        "kms:EncryptionContext:aws:logs:arn": (
                            f"arn:{self.partition}:logs:{self.region}:{self.account}:"
                            f"log-group:/loan-tape-dq/{stage}/detector"
                        )
                    }
                },
            )
        )

        detector = lambda_.DockerImageFunction(
            self,
            "Detector",
            code=lambda_.DockerImageCode.from_image_asset(
                REPO_ROOT,
                file="infra/lambda/Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64,
                ignore_mode=IgnoreMode.GLOB,
                exclude=[
                    "**",
                    "!pyproject.toml",
                    "!README.md",
                    "!requirements-runtime.lock",
                    "!src",
                    "!src/**",
                    "!config",
                    "!config/**",
                    "!infra",
                    "!infra/lambda",
                    "!infra/lambda/**",
                    "**/__pycache__",
                    "**/*.pyc",
                ],
            ),
            architecture=lambda_.Architecture.ARM_64,
            memory_size=2048,
            ephemeral_storage_size=Size.gibibytes(2),
            timeout=Duration.minutes(10),
            environment={
                "RESULTS_BUCKET": results.bucket_name,
                "RAW_BUCKET": raw_tapes.bucket_name,
                "RUN_TABLE": runs.table_name,
                "STAGE": stage,
                "MAX_INPUT_BYTES": str(50 * 1024 * 1024),
                "LEASE_SECONDS": "660",
                "MAX_RUNTIME_SECONDS": "600",
            },
            dead_letter_queue=dead_letter,
            retry_attempts=2,
            max_event_age=Duration.hours(6),
            log_group=detector_logs,
        )
        detector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObjectVersion"], resources=[raw_tapes.arn_for_objects("*")]
            )
        )
        data_key.grant_decrypt(detector)
        results.grant_put(detector, "diagnostics/runs/*")
        runs.grant_read_write_data(detector)
        # the handler publishes the circuit-breaker metric
        detector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "LoanTapeDQ"}},
            )
        )

        # --- ingestion trigger --------------------------------------------
        events.Rule(
            self,
            "OnTapeUploaded",
            rule_name=f"loan-tape-dq-{stage}-uploads",
            description="run the detector when a lender drops a tape in the raw bucket",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [raw_tapes.bucket_name]},
                    "object": {"key": [{"suffix": s} for s in TAPE_SUFFIXES]},
                },
            ),
            targets=[
                targets.LambdaFunction(
                    detector,
                    dead_letter_queue=dead_letter,
                    retry_attempts=2,
                    max_event_age=Duration.hours(2),
                )
            ],
        )

        for service, source_arn in (
            (
                "events.amazonaws.com",
                f"arn:{self.partition}:events:{self.region}:{self.account}:"
                f"rule/loan-tape-dq-{stage}-uploads",
            ),
            (
                "cloudwatch.amazonaws.com",
                f"arn:{self.partition}:cloudwatch:{self.region}:{self.account}:"
                f"alarm:loan-tape-dq-{stage}-*",
            ),
        ):
            data_key.add_to_resource_policy(
                iam.PolicyStatement(
                    principals=[iam.ServicePrincipal(service)],
                    actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                    resources=["*"],
                    conditions={
                        "StringEquals": {"aws:SourceAccount": self.account},
                        "ArnLike": {"aws:SourceArn": source_arn},
                    },
                )
            )

        # --- monitoring ---------------------------------------------------
        detector.metric_errors(period=Duration.minutes(5)).create_alarm(
            self,
            "DetectorErrorAlarm",
            alarm_name=f"loan-tape-dq-{stage}-detector-error",
            alarm_description="detector Lambda raised an unhandled error",
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(alerts))

        dead_letter.metric_approximate_number_of_messages_visible().create_alarm(
            self,
            "DeadLetterNotEmptyAlarm",
            alarm_name=f"loan-tape-dq-{stage}-dead-letter",
            alarm_description="an ingestion event failed every retry and landed in the DLQ",
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(alerts))

        # The detector emits this metric when a tape trips the anomaly-rate circuit
        # breaker (>40% flagged high/critical); alarm holds the tape for a human.
        cw.Metric(
            namespace="LoanTapeDQ",
            metric_name="CircuitBreakerTripped",
            dimensions_map={"Stage": stage},
            period=Duration.minutes(5),
            statistic="Sum",
        ).create_alarm(
            self,
            "CircuitBreakerAlarm",
            alarm_name=f"loan-tape-dq-{stage}-circuit-breaker",
            alarm_description="a tape tripped the anomaly-rate circuit breaker; hold and review",
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(alerts))

        CfnOutput(self, "RawTapesBucket", value=raw_tapes.bucket_name)
        CfnOutput(self, "ResultsBucket", value=results.bucket_name)
        CfnOutput(self, "RunTable", value=runs.table_name)
        CfnOutput(self, "DetectorFunctionName", value=detector.function_name)
        CfnOutput(self, "AlertsTopicArn", value=alerts.topic_arn)
