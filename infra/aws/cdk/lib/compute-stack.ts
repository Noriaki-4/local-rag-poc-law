import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import { EnvironmentConfig, resourcePrefix } from "./config";

export interface ComputeStackProps extends cdk.StackProps {
  readonly config: EnvironmentConfig;
  readonly vpc: ec2.IVpc;
}

export class ComputeStack extends cdk.Stack {
  public readonly cluster: ecs.Cluster;
  public readonly repositories: Readonly<Record<string, ecr.Repository>>;
  public readonly logGroups: Readonly<Record<string, logs.LogGroup>>;

  public constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    const prefix = resourcePrefix(props.config);
    const repositoryRemovalPolicy = props.config.compute
      .retainRepositoriesOnDelete
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;

    this.cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: `${prefix}-cluster`,
      vpc: props.vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    const repositories: Record<string, ecr.Repository> = {};
    const logGroups: Record<string, logs.LogGroup> = {};
    for (const component of props.config.compute.repositoryNames) {
      const constructId = toConstructId(component);
      const repository = new ecr.Repository(this, `${constructId}Repository`, {
        repositoryName: `${prefix}/${component}`,
        encryption: ecr.RepositoryEncryption.AES_256,
        imageScanOnPush: true,
        imageTagMutability: ecr.TagMutability.IMMUTABLE,
        lifecycleRules: [
          {
            description: "Retain the most recent deployable images",
            maxImageCount: props.config.compute.lifecycleMaxImageCount,
            rulePriority: 1,
          },
        ],
        removalPolicy: repositoryRemovalPolicy,
        emptyOnDelete: !props.config.compute.retainRepositoriesOnDelete,
      });
      const logGroup = new logs.LogGroup(this, `${constructId}LogGroup`, {
        logGroupName: `/${props.config.projectName}/${props.config.environmentName}/${component}`,
        retention: toLogRetention(props.config.compute.logRetentionDays),
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      repositories[component] = repository;
      logGroups[component] = logGroup;

      new cdk.CfnOutput(this, `${constructId}RepositoryUri`, {
        value: repository.repositoryUri,
        description: `ECR repository URI for ${component}`,
      });
      new cdk.CfnOutput(this, `${constructId}LogGroupName`, {
        value: logGroup.logGroupName,
        description: `CloudWatch Logs group for ${component}`,
      });
    }
    this.repositories = repositories;
    this.logGroups = logGroups;

    new cdk.CfnOutput(this, "ClusterName", {
      value: this.cluster.clusterName,
      description: "ECS cluster for legal RAG workloads",
    });
  }
}

function toConstructId(value: string): string {
  return value
    .split("-")
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join("");
}

function toLogRetention(days: number): logs.RetentionDays {
  const byDays = new Map<number, logs.RetentionDays>([
    [1, logs.RetentionDays.ONE_DAY],
    [3, logs.RetentionDays.THREE_DAYS],
    [5, logs.RetentionDays.FIVE_DAYS],
    [7, logs.RetentionDays.ONE_WEEK],
    [14, logs.RetentionDays.TWO_WEEKS],
    [30, logs.RetentionDays.ONE_MONTH],
    [60, logs.RetentionDays.TWO_MONTHS],
    [90, logs.RetentionDays.THREE_MONTHS],
    [120, logs.RetentionDays.FOUR_MONTHS],
    [150, logs.RetentionDays.FIVE_MONTHS],
    [180, logs.RetentionDays.SIX_MONTHS],
    [365, logs.RetentionDays.ONE_YEAR],
    [400, logs.RetentionDays.THIRTEEN_MONTHS],
    [545, logs.RetentionDays.EIGHTEEN_MONTHS],
    [731, logs.RetentionDays.TWO_YEARS],
    [1827, logs.RetentionDays.FIVE_YEARS],
    [3653, logs.RetentionDays.TEN_YEARS],
  ]);
  const retention = byDays.get(days);
  if (retention === undefined) {
    throw new Error(`Unsupported CloudWatch Logs retention: ${days}`);
  }
  return retention;
}
