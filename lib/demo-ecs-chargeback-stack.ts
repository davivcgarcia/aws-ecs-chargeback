import * as cdk from '@aws-cdk/core';
import * as iam from '@aws-cdk/aws-iam';
import * as ec2 from '@aws-cdk/aws-ec2';
import * as ecs from '@aws-cdk/aws-ecs';
import * as lambda from '@aws-cdk/aws-lambda';
import * as dynamodb from '@aws-cdk/aws-dynamodb';
import * as events from '@aws-cdk/aws-events';
import * as targets from '@aws-cdk/aws-events-targets';

export class DemoEcsChargebackStack extends cdk.Stack {
  constructor(scope: cdk.Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const cluster = new ecs.Cluster(this, 'ChargebackDemoCluster', {
      clusterName: 'ChargebackDemoCluster',
      enableFargateCapacityProviders: true,
      capacity: {
        instanceType: new ec2.InstanceType("t3.medium"),
        desiredCapacity: 2,
      }
    });

    const genericTask = new ecs.TaskDefinition(this, 'GenericTask', {
      compatibility: ecs.Compatibility.EC2_AND_FARGATE,
      cpu: '256',
      memoryMiB: '512'
    });
    genericTask.addContainer('nginx', {
      image: ecs.ContainerImage.fromRegistry('public.ecr.aws/nginx/nginx:latest'),
      cpu: 256,
      memoryReservationMiB: 512,
      portMappings: [{
        containerPort: 80
      }]
    });

    const ec2Service = new ecs.Ec2Service(this, 'Ec2Service', {
      serviceName: 'Ec2Service',
      cluster: cluster,
      taskDefinition: genericTask,
      propagateTags: ecs.PropagatedTagSource.NONE,
      enableECSManagedTags: false,
      desiredCount: 4,
    });

    const fargateService = new ecs.FargateService(this, 'FargateService', {
      serviceName: 'FargateService',
      cluster: cluster,
      taskDefinition: genericTask,
      propagateTags: ecs.PropagatedTagSource.NONE,
      enableECSManagedTags: false,
      desiredCount: 2,
    });

    const meteringDb = new dynamodb.Table(this, 'MeteringDB', {
      tableName: 'ECSTaskStatus',
      partitionKey: { 
        name: 'taskArn',
        type: dynamodb.AttributeType.STRING
      },
      readCapacity: 10,
      writeCapacity: 20
    });

    const statusLambda = new lambda.Function(this, 'TaskStatus', {
      functionName: 'ecsTaskStatus',
      code: lambda.Code.fromAsset('lambda'),
      runtime: lambda.Runtime.PYTHON_3_6,
      handler: 'ecsTaskStatus.lambda_handler',
    });

    statusLambda.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'ecs:DescribeContainerInstances'
      ],
      resources: ['*']
    }));

    statusLambda.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:PutLogEvents'
      ],
      resources: ['arn:aws:logs:*:*:*']
    }));

    meteringDb.grantReadWriteData(statusLambda);

    const eventRule = new events.Rule(this, 'EcsEventRule', {
      eventPattern: {
        source: ['aws.ecs'],
        detailType: ['ECS Task State Change'],
        detail: {
          lastStatus: ["RUNNING", "STOPPED"]
        },
      },
      targets: [new targets.LambdaFunction(statusLambda)]
    })

  }
}