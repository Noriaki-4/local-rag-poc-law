import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as neptunegraph from "aws-cdk-lib/aws-neptunegraph";
import * as aoss from "aws-cdk-lib/aws-opensearchserverless";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { EnvironmentConfig, openSearchServerlessResourceName } from "./config";

export interface DataStackProps extends cdk.StackProps {
  readonly config: EnvironmentConfig;
  readonly vpc: ec2.IVpc;
}

export class DataStack extends cdk.Stack {
  public readonly knowledgeBucket: s3.Bucket;
  public readonly openSearchCollection: aoss.CfnCollection;
  public readonly openSearchVpcEndpoint: aoss.CfnVpcEndpoint;
  public readonly neptuneGraph: neptunegraph.CfnGraph;
  public readonly neptunePrivateEndpoint: neptunegraph.CfnPrivateGraphEndpoint;
  public readonly bootstrapAdminRole: iam.Role;

  public constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    const retainData = props.config.data.retainOnDelete;

    this.knowledgeBucket = new s3.Bucket(this, "KnowledgeBucket", {
      bucketName: props.config.data.bucketName ?? undefined,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      removalPolicy: retainData
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: !retainData,
      lifecycleRules: [
        {
          id: "artifact-lifecycle",
          enabled: true,
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
          noncurrentVersionExpiration: cdk.Duration.days(
            props.config.data.noncurrentVersionExpirationDays,
          ),
        },
      ],
    });

    new cdk.CfnOutput(this, "KnowledgeBucketName", {
      value: this.knowledgeBucket.bucketName,
      description:
        "S3 bucket for source documents, derived artifacts, and evaluation data",
    });
    new cdk.CfnOutput(this, "KnowledgeBucketArn", {
      value: this.knowledgeBucket.bucketArn,
      description: "ARN of the legal RAG knowledge bucket",
    });

    const endpointSecurityGroup = new ec2.SecurityGroup(
      this,
      "OpenSearchEndpointSecurityGroup",
      {
        vpc: props.vpc,
        description:
          "HTTPS access to the private OpenSearch Serverless VPC endpoint",
        allowAllOutbound: false,
      },
    );
    endpointSecurityGroup.addIngressRule(
      ec2.Peer.ipv4(props.config.network.cidr),
      ec2.Port.tcp(443),
      "HTTPS from workloads in the legal RAG VPC",
    );

    this.openSearchVpcEndpoint = new aoss.CfnVpcEndpoint(
      this,
      "OpenSearchVpcEndpoint",
      {
        name: openSearchServerlessResourceName(
          props.config.openSearchServerless.collectionName,
          "vpce",
        ),
        vpcId: props.vpc.vpcId,
        subnetIds: props.vpc.selectSubnets({ subnetGroupName: "data" })
          .subnetIds,
        securityGroupIds: [endpointSecurityGroup.securityGroupId],
      },
    );

    const collectionName = props.config.openSearchServerless.collectionName;
    const encryptionPolicy = new aoss.CfnSecurityPolicy(
      this,
      "OpenSearchEncryptionPolicy",
      {
        name: openSearchServerlessResourceName(collectionName, "encryption"),
        type: "encryption",
        description: "AWS-owned encryption key for the legal RAG collection",
        policy: JSON.stringify({
          Rules: [
            {
              ResourceType: "collection",
              Resource: [`collection/${collectionName}`],
            },
          ],
          AWSOwnedKey: true,
        }),
      },
    );

    const networkPolicy = new aoss.CfnSecurityPolicy(
      this,
      "OpenSearchNetworkPolicy",
      {
        name: openSearchServerlessResourceName(collectionName, "network"),
        type: "network",
        description:
          "Private collection access through the environment VPC endpoint",
        policy: cdk.Fn.toJsonString([
          {
            Description: "Private API access for the legal RAG collection",
            Rules: [
              {
                ResourceType: "collection",
                Resource: [`collection/${collectionName}`],
              },
            ],
            AllowFromPublic: false,
            SourceVPCEs: [this.openSearchVpcEndpoint.attrId],
          },
        ]),
      },
    );

    this.openSearchCollection = new aoss.CfnCollection(
      this,
      "OpenSearchCollection",
      {
        name: collectionName,
        description: "Private vector and full-text search for legal RAG",
        type: "VECTORSEARCH",
        standbyReplicas: props.config.openSearchServerless.standbyReplicas,
        deletionProtection: props.config.openSearchServerless.retainOnDelete
          ? "ENABLED"
          : "DISABLED",
      },
    );
    this.openSearchCollection.node.addDependency(encryptionPolicy);
    this.openSearchCollection.node.addDependency(networkPolicy);
    this.openSearchCollection.applyRemovalPolicy(
      props.config.openSearchServerless.retainOnDelete
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
    );

    new cdk.CfnOutput(this, "OpenSearchCollectionArn", {
      value: this.openSearchCollection.attrArn,
      description: "ARN of the OpenSearch Serverless collection",
    });
    new cdk.CfnOutput(this, "OpenSearchCollectionEndpoint", {
      value: this.openSearchCollection.attrCollectionEndpoint,
      description: "Private OpenSearch Serverless data endpoint",
    });
    new cdk.CfnOutput(this, "OpenSearchVpcEndpointId", {
      value: this.openSearchVpcEndpoint.attrId,
      description: "OpenSearch Serverless-managed VPC endpoint ID",
    });

    this.neptuneGraph = new neptunegraph.CfnGraph(this, "LegalGraph", {
      graphName: props.config.neptuneAnalytics.graphName,
      provisionedMemory: props.config.neptuneAnalytics.provisionedMemory,
      replicaCount: props.config.neptuneAnalytics.replicaCount,
      publicConnectivity: false,
      deletionProtection: props.config.neptuneAnalytics.retainOnDelete,
    });
    this.neptuneGraph.applyRemovalPolicy(
      props.config.neptuneAnalytics.retainOnDelete
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
    );

    const graphEndpointSecurityGroup = new ec2.SecurityGroup(
      this,
      "NeptuneEndpointSecurityGroup",
      {
        vpc: props.vpc,
        description: "HTTPS access to the private Neptune Analytics endpoint",
        allowAllOutbound: false,
      },
    );
    graphEndpointSecurityGroup.addIngressRule(
      ec2.Peer.ipv4(props.config.network.cidr),
      ec2.Port.tcp(443),
      "HTTPS from workloads in the legal RAG VPC",
    );
    this.neptunePrivateEndpoint = new neptunegraph.CfnPrivateGraphEndpoint(
      this,
      "NeptunePrivateGraphEndpoint",
      {
        graphIdentifier: this.neptuneGraph.attrGraphId,
        vpcId: props.vpc.vpcId,
        subnetIds: props.vpc.selectSubnets({ subnetGroupName: "data" })
          .subnetIds,
        securityGroupIds: [graphEndpointSecurityGroup.securityGroupId],
      },
    );
    this.neptunePrivateEndpoint.node.addDependency(this.neptuneGraph);

    this.bootstrapAdminRole = new iam.Role(this, "BootstrapAdminRole", {
      roleName: `${props.config.projectName}-${props.config.environmentName}-bootstrap`,
      assumedBy: new iam.AccountRootPrincipal(),
      description:
        "Explicitly assumed role for S3, OpenSearch, Bedrock, and Neptune bootstrap writes",
      maxSessionDuration: cdk.Duration.hours(2),
    });
    this.knowledgeBucket.grantRead(this.bootstrapAdminRole);
    this.knowledgeBucket.grantPut(this.bootstrapAdminRole);
    this.bootstrapAdminRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "InvokeTitanEmbedding",
        actions: ["bedrock:InvokeModel"],
        resources: [
          `arn:${cdk.Aws.PARTITION}:bedrock:${props.config.region}::foundation-model/${props.config.openSearchServerless.embeddingModelId}`,
        ],
      }),
    );
    this.bootstrapAdminRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "OpenSearchServerlessDataPlane",
        actions: ["aoss:APIAccessAll"],
        resources: [this.openSearchCollection.attrArn],
      }),
    );
    this.bootstrapAdminRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "WriteNeptuneAnalyticsBootstrap",
        actions: [
          "neptune-graph:GetGraph",
          "neptune-graph:ReadDataViaQuery",
          "neptune-graph:WriteDataViaQuery",
        ],
        resources: [this.neptuneGraph.attrGraphArn],
      }),
    );

    new aoss.CfnAccessPolicy(this, "OpenSearchBootstrapDataAccessPolicy", {
      name: openSearchServerlessResourceName(collectionName, "bootstrap"),
      type: "data",
      description: "Write access for the explicitly assumed bootstrap role",
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
              Resource: [`index/${collectionName}/*`],
              Permission: [
                "aoss:CreateIndex",
                "aoss:DescribeIndex",
                "aoss:UpdateIndex",
                "aoss:WriteDocument",
                "aoss:ReadDocument",
              ],
            },
          ],
          Principal: [this.bootstrapAdminRole.roleArn],
        },
      ]),
    });

    new cdk.CfnOutput(this, "NeptuneGraphId", {
      value: this.neptuneGraph.attrGraphId,
      description: "Neptune Analytics graph ID",
    });
    new cdk.CfnOutput(this, "NeptuneGraphArn", {
      value: this.neptuneGraph.attrGraphArn,
      description: "Neptune Analytics graph ARN",
    });
    new cdk.CfnOutput(this, "NeptuneGraphEndpoint", {
      value: this.neptuneGraph.attrEndpoint,
      description: "Private Neptune Analytics graph endpoint",
    });
    new cdk.CfnOutput(this, "NeptunePrivateGraphEndpointId", {
      value: this.neptunePrivateEndpoint.attrPrivateGraphEndpointIdentifier,
      description: "Private graph endpoint identifier",
    });
    new cdk.CfnOutput(this, "BootstrapAdminRoleArn", {
      value: this.bootstrapAdminRole.roleArn,
      description: "Role to assume for the explicit bootstrap workflow",
    });
  }
}
