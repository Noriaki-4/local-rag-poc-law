import * as cdk from "aws-cdk-lib";
import * as agentcore from "aws-cdk-lib/aws-bedrockagentcore";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as neptunegraph from "aws-cdk-lib/aws-neptunegraph";
import * as aoss from "aws-cdk-lib/aws-opensearchserverless";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { EnvironmentConfig, openSearchServerlessResourceName } from "./config";

export interface RuntimeStackProps extends cdk.StackProps {
  readonly config: EnvironmentConfig;
  readonly vpc: ec2.IVpc;
  readonly repositories: Readonly<Record<string, ecr.Repository>>;
  readonly knowledgeBucket: s3.IBucket;
  readonly openSearchCollection: aoss.CfnCollection;
  readonly neptuneGraph: neptunegraph.CfnGraph;
}

export class RuntimeStack extends cdk.Stack {
  public readonly runtime: agentcore.Runtime;
  public readonly runtimeSecurityGroup?: ec2.SecurityGroup;

  public constructor(scope: Construct, id: string, props: RuntimeStackProps) {
    super(scope, id, props);

    const repository =
      props.repositories[props.config.agentCore.imageRepositoryName];
    if (repository === undefined) {
      throw new Error(
        `AgentCore repository is not available: ${props.config.agentCore.imageRepositoryName}`,
      );
    }

    let networkConfiguration: agentcore.RuntimeNetworkConfiguration;
    if (props.config.agentCore.networkMode === "VPC") {
      const securityGroup = new ec2.SecurityGroup(
        this,
        "RuntimeSecurityGroup",
        {
          vpc: props.vpc,
          description: "Outbound access for the legal RAG AgentCore Runtime",
          allowAllOutbound: true,
        },
      );
      // AgentCoreが作成したENIの削除には時間差があり、stack削除時のSG削除が
      // 失敗し得るため保持する。SG IDをoutputし、手動削除対象を特定可能にする。
      securityGroup.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
      cdk.Tags.of(securityGroup).add("ManualCleanupRequired", "true");
      cdk.Tags.of(securityGroup).add(
        "CleanupReason",
        "AgentCoreManagedEniDependency",
      );
      this.runtimeSecurityGroup = securityGroup;
      networkConfiguration = agentcore.RuntimeNetworkConfiguration.usingVpc(
        this,
        {
          vpc: props.vpc,
          vpcSubnets: { subnetGroupName: "application" },
          securityGroups: [securityGroup],
        },
      );
    } else {
      networkConfiguration =
        agentcore.RuntimeNetworkConfiguration.usingPublicNetwork();
    }

    this.runtime = new agentcore.Runtime(this, "LegalAgentRuntime", {
      runtimeName: props.config.agentCore.runtimeName,
      description: "Legal RAG backend invoked by GenU",
      protocolConfiguration: agentcore.ProtocolType.HTTP,
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromEcrRepository(
        repository,
        props.config.agentCore.imageTag,
      ),
      networkConfiguration,
      environmentVariables: {
        AGENTCORE_RUNTIME: "true",
        AWS_REGION: props.config.region,
        LLM_PROVIDER: "bedrock",
        BEDROCK_MODEL_ID: props.config.bedrock.generationModelId,
        RERANK_PROVIDER: "none",
        KNOWLEDGE_BUCKET_NAME: props.knowledgeBucket.bucketName,
        OPENSEARCH_MODE: "serverless",
        OPENSEARCH_URL: props.openSearchCollection.attrCollectionEndpoint,
        OPENSEARCH_INDEX: props.config.openSearchServerless.indexName,
        OPENSEARCH_AWS_REGION: props.config.region,
        OPENSEARCH_AWS_SERVICE: "aoss",
        EMBEDDING_PROVIDER: "bedrock",
        EMBEDDING_MODEL: props.config.openSearchServerless.embeddingModelId,
        EMBEDDING_DIMENSION: String(
          props.config.openSearchServerless.embeddingDimensions,
        ),
        EMBEDDING_NORMALIZE: String(
          props.config.openSearchServerless.embeddingNormalize,
        ).toLowerCase(),
        EMBEDDING_MAX_CHARS: String(
          props.config.openSearchServerless.embeddingMaxChars,
        ),
        LLM_MODEL: props.config.bedrock.generationModelId,
        GRAPH_PROVIDER: "neptune-analytics",
        NEPTUNE_GRAPH_ID: props.neptuneGraph.attrGraphId,
        NEPTUNE_GRAPH_ENDPOINT: props.neptuneGraph.attrEndpoint,
        LEGAL_RELATION_CLASSIFICATION_RUN_ID:
          props.config.bootstrapData.classificationRunId,
        EVAL_RESULTS_DIR: "/tmp/eval-results",
      },
      tracingEnabled: true,
    });

    props.knowledgeBucket.grantRead(this.runtime.role);
    const generationBaseModelId =
      props.config.bedrock.generationModelId.replace(/^jp\./, "");
    this.runtime.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "InvokeFoundationModels",
        actions: ["bedrock:InvokeModel"],
        resources: [
          `arn:${cdk.Aws.PARTITION}:bedrock:${props.config.region}:${props.config.account}:inference-profile/${props.config.bedrock.generationModelId}`,
          `arn:${cdk.Aws.PARTITION}:bedrock:ap-northeast-1::foundation-model/${generationBaseModelId}`,
          `arn:${cdk.Aws.PARTITION}:bedrock:ap-northeast-3::foundation-model/${generationBaseModelId}`,
          `arn:${cdk.Aws.PARTITION}:bedrock:${props.config.region}::foundation-model/${props.config.openSearchServerless.embeddingModelId}`,
        ],
      }),
    );
    this.runtime.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "ReadNeptuneAnalytics",
        actions: ["neptune-graph:GetGraph", "neptune-graph:ReadDataViaQuery"],
        resources: [props.neptuneGraph.attrGraphArn],
      }),
    );
    this.runtime.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "OpenSearchServerlessDataPlane",
        actions: ["aoss:APIAccessAll"],
        resources: [props.openSearchCollection.attrArn],
      }),
    );

    const collectionName = props.config.openSearchServerless.collectionName;
    new aoss.CfnAccessPolicy(this, "OpenSearchRuntimeDataAccessPolicy", {
      name: openSearchServerlessResourceName(collectionName, "runtime"),
      type: "data",
      description: "Read-only collection access for the Legal Agent Runtime",
      policy: cdk.Fn.toJsonString([
        {
          Rules: [
            {
              ResourceType: "collection",
              Resource: [`collection/${collectionName}`],
              Permission: ["aoss:DescribeCollectionItems"],
            },
            {
              ResourceType: "index",
              Resource: [
                `index/${collectionName}/${props.config.openSearchServerless.indexName}`,
              ],
              Permission: ["aoss:DescribeIndex", "aoss:ReadDocument"],
            },
          ],
          Principal: [this.runtime.role.roleArn],
        },
      ]),
    });

    new cdk.CfnOutput(this, "AgentCoreRuntimeArn", {
      value: this.runtime.agentRuntimeArn,
      description: "ARN to register in GenU agentCoreExternalRuntimes",
    });
    new cdk.CfnOutput(this, "AgentCoreRuntimeName", {
      value: this.runtime.agentRuntimeName,
      description: "Bedrock AgentCore Runtime name",
    });
    new cdk.CfnOutput(this, "AgentCoreExecutionRoleArn", {
      value: this.runtime.role.roleArn,
      description: "Execution role used by the Legal Agent Runtime",
    });
    new cdk.CfnOutput(this, "AgentCoreImage", {
      value: `${repository.repositoryUri}:${props.config.agentCore.imageTag}`,
      description: "Immutable ECR image expected by the AgentCore Runtime",
    });
    new cdk.CfnOutput(this, "OpenSearchCollectionEndpoint", {
      value: props.openSearchCollection.attrCollectionEndpoint,
      description: "OpenSearch Serverless endpoint configured on the Runtime",
    });
    new cdk.CfnOutput(this, "NeptuneGraphEndpoint", {
      value: props.neptuneGraph.attrEndpoint,
      description: "Private Neptune Analytics endpoint configured on Runtime",
    });
    if (this.runtimeSecurityGroup !== undefined) {
      new cdk.CfnOutput(this, "RetainedRuntimeSecurityGroupId", {
        value: this.runtimeSecurityGroup.securityGroupId,
        description:
          "Retained SG; delete manually after AgentCore-managed ENIs are gone",
      });
    }
  }
}
