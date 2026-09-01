import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";
import { EnvironmentConfig, resourcePrefix } from "./config";

export interface NetworkStackProps extends cdk.StackProps {
  readonly config: EnvironmentConfig;
}

export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;

  public constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    const prefix = resourcePrefix(props.config);
    const applicationSubnetType =
      props.config.network.natGateways > 0
        ? ec2.SubnetType.PRIVATE_WITH_EGRESS
        : ec2.SubnetType.PRIVATE_ISOLATED;

    this.vpc = new ec2.Vpc(this, "Vpc", {
      vpcName: `${prefix}-vpc`,
      ipAddresses: ec2.IpAddresses.cidr(props.config.network.cidr),
      availabilityZones: Array.from(
        { length: props.config.network.availabilityZoneIds.length },
        (_, index) => cdk.Fn.select(index, cdk.Fn.getAzs()),
      ),
      natGateways: props.config.network.natGateways,
      restrictDefaultSecurityGroup: true,
      subnetConfiguration: [
        {
          name: "public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: "application",
          subnetType: applicationSubnetType,
          cidrMask: 24,
        },
        {
          name: "data",
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
    });

    pinSubnetsToAvailabilityZoneIds(
      this.vpc.publicSubnets,
      props.config.network.availabilityZoneIds,
    );
    pinSubnetsToAvailabilityZoneIds(
      this.vpc.privateSubnets,
      props.config.network.availabilityZoneIds,
    );
    pinSubnetsToAvailabilityZoneIds(
      this.vpc.isolatedSubnets,
      props.config.network.availabilityZoneIds,
    );

    this.vpc.addGatewayEndpoint("S3GatewayEndpoint", {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });

    new cdk.CfnOutput(this, "VpcId", {
      value: this.vpc.vpcId,
      description: "VPC used by the legal RAG PoC",
    });
    new cdk.CfnOutput(this, "ApplicationSubnetIds", {
      value: cdk.Fn.join(
        ",",
        this.vpc.selectSubnets({ subnetGroupName: "application" }).subnetIds,
      ),
      description: "Subnets for application workloads",
    });
    new cdk.CfnOutput(this, "DataSubnetIds", {
      value: cdk.Fn.join(
        ",",
        this.vpc.selectSubnets({ subnetGroupName: "data" }).subnetIds,
      ),
      description: "Isolated subnets for data services",
    });
  }
}

function pinSubnetsToAvailabilityZoneIds(
  subnets: readonly ec2.ISubnet[],
  availabilityZoneIds: readonly string[],
): void {
  if (subnets.length !== availabilityZoneIds.length) {
    throw new Error(
      `Expected ${availabilityZoneIds.length} subnets, received ${subnets.length}`,
    );
  }
  subnets.forEach((subnet, index) => {
    const cfnSubnet = subnet.node.defaultChild as ec2.CfnSubnet;
    cfnSubnet.availabilityZone = undefined;
    cfnSubnet.availabilityZoneId = availabilityZoneIds[index];
  });
}
