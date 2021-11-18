import json
import os
import boto3
import jmespath
import random
from datetime import datetime, timedelta
from dateutil import parser

from botocore.exceptions import ClientError

ec2 = boto3.client('ec2')


def list_nat_gateways():
    filters = [
        {'Name': 'state', 'Values': ['pending', 'available']},
        {'Name': 'vpc-id', 'Values': [os.environ['VPC_ID']]}
    ]

    gateway_json = ec2.describe_nat_gateways(Filters=filters)
    gateways = jmespath.search(
        'NatGateways[*].[NatGatewayId, State, CreateTime, Tags[?Key==\'LastRequested\'].Value | [0]]', gateway_json)

    if len(gateways) > 0:
        print("Checking for existing gateways, found ---\n%s\n---\n" % gateways)
        return gateways
    else:
        print("Checking for existing gateways, found none\n")
        return None


def create_nat_gateway():
    alloc_json = ec2.describe_addresses(Filters=[
        {'Name': 'tag:Name', 'Values': ['OnDemandNAT-IPAddr']},
        {'Name': 'tag:ForVpc', 'Values': [os.environ['VPC_NAME']]}
    ])
    allocId = jmespath.search('Addresses[0].AllocationId', alloc_json)

    subnet_json = ec2.describe_subnets(Filters=[
        {'Name': 'tag:Public', 'Values': ['Yes']},
        {'Name': 'vpc-id', 'Values': [os.environ['VPC_ID']]}
    ])
    subnet_list = jmespath.search('Subnets[*].SubnetId', subnet_json)
    subnetId = random.choice(subnet_list)

    new_gw_json = ec2.create_nat_gateway(
        AllocationId=allocId, SubnetId=subnetId)
    gatewayId = jmespath.search('NatGateway.NatGatewayId', new_gw_json)

    print('NAT Gateway Created\n\tID: %s\tInfo ---\n%s\n---\n' %
          (gatewayId, new_gw_json))

    ec2.create_tags(
        Resources=[gatewayId], Tags=[
            {'Key': 'OnDemandNAT', 'Value': 'True'}, {'Key': 'Name', 'Value': 'OnDemandNAT-Gateway'}, {'Key': 'LastRequested', 'Value': '%s' % datetime.utcnow(
            )}, {'Key': 'ForVpc', 'Value': os.environ['VPC_NAME']}, {'Key': 'Application', 'Value': 'OnDemandNAT'}, {'Key': 'Environment', 'Value': 'Infrastructure'}
        ]
    )

    # Wait for gateway to finish starting.
    waiter = ec2.get_waiter('nat_gateway_available')
    waiter.wait(NatGatewayIds=[gatewayId])

    return gatewayId


def update_route_tables(gatewayId):
    routes_json = ec2.describe_route_tables(
        Filters=[{'Name': 'tag:OnDemandNAT', 'Values': ['Yes', 'True']}])
    routes_list = jmespath.search('RouteTables[*].RouteTableId', routes_json)

    print("Fetched Routes List for update ---\n%s\n---\n" % routes_list)

    for routeTableId in routes_list:
        print("Updating Route Table %s" % routeTableId)
        try:
            ec2.delete_route(RouteTableId=routeTableId,
                             DestinationCidrBlock='0.0.0.0/0')
        except ClientError as e:
            # We expect the occasional failure where the route doesn't exist - this can be safely ignored.
            if e.response['Error']['Code'] != 'InvalidRoute.NotFound':
                raise e
        ec2.create_route(RouteTableId=routeTableId,
                         DestinationCidrBlock='0.0.0.0/0', NatGatewayId=gatewayId)

        print("Update Completed for %s\n" % routeTableId)


def invoke_lambda(payload):
    client = boto3.client('lambda')
    response = client.invoke(
        FunctionName="afipApi",
        InvocationType='Event',
        Payload=payload
    )
    print(response)


def request_gateway_handler(event, context):
    print("NAT Gateway Requested\n")
    try:
        gateway_list = list_nat_gateways()

        info = {
            'statusCode': 200
        }

        if gateway_list == None:
            print("New Gateway Required, Launching\n")
            gatewayId = create_nat_gateway()
            update_route_tables(gatewayId)
        else:
            print("NAT Gateway already provisioned - updating Last Requested Timestamp\n")
            info['nat-existing'] = True
            for (gatewayId, state, created, lastRequested) in gateway_list:
                ec2.create_tags(
                    Resources=[gatewayId], Tags=[
                        {'Key': 'LastRequested', 'Value': '%s' % datetime.utcnow()}]
                )

        if 'CodePipeline.job' in event:
            job = event['CodePipeline.job']

            cp = boto3.client('codepipeline')
            cp.put_job_success_result(
                jobId=job['id']
            )

        print("SUMMARY:\n%s\n" % json.dumps(info))
        invoke_lambda(event['Records'][0]['body'])
        return info
    except BaseException as e:
        if 'CodePipeline.job' in event:
            job = event['CodePipeline.job']

            cp = boto3.client('codepipeline')
            cp.put_job_failure_result(
                jobId=job['id'],
                failureDetails={
                    "type": "JobFailed",  "message": '%s' % e
                }
            )
        raise e


def check_gateway_required(event, context):
    gateway_list = list_nat_gateways()

    info = {}
    gw_change_list = []

    if gateway_list == None:
        # Nothing to do.
        print("No Gateway running, nothing to do")
        return

    print("Active gateway detected, checking gateway ages")
    for (gatewayId, state, created, lastRequested) in gateway_list:
        age = datetime.now(created.tzinfo) - created

        # If we have a last requested date, we use that, but if not we fall back
        # to using the age of the gateway
        if lastRequested != None:
            inactive = datetime.utcnow() - parser.isoparse(lastRequested)
        else:
            inactive = age

        if inactive >= timedelta(minutes=45):
            ec2.delete_nat_gateway(NatGatewayId=gatewayId)
            print("Gateway %s detected as inactive, terminated" % gatewayId)
            gw_change_list.append({'action': 'deleted', 'gatewayId': gatewayId, 'age': (
                '%s' % age), 'inactive': ('%s' % inactive)})
        else:
            print("Gateway %s is still active, skipped" % gatewayId)
            gw_change_list.append({'action': 'skipped', 'gatewayId': gatewayId, 'age': (
                '%s' % age), 'inactive': ('%s' % inactive)})
    info['nat-changed'] = gw_change_list
    invoke_lambda(event['Records'][0]['body'])

    print("SUMMARY:\n%s\n" % json.dumps(info))
    return info
