#!/usr/bin/env python

# Copyright 2015 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file.
# This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and limitations under the License.

#
# NOTE: This code witll work with Python3 only. Some of the code is not compatible
#       to run on Python2.7.

import boto3
from boto3.dynamodb.conditions import Key, Attr
import ast
from argparse import ArgumentParser
import datetime
from dateutil.tz import *
from dateutil.relativedelta import *
import re
import sys
import logging
import json

cpu2mem_weight = 0.5
pricing_dict = {}
region_table = {}

def get(table, region, cluster, service):
    """
    Scan the DynamoDB table to get all tasks in a service.
    Input - region, ECS ClusterARN and ECS ServiceName
    """
    resp = table.scan(
        FilterExpression=Attr('group').eq('service') &
                Attr('groupName').eq(service) &
                Attr('region').eq(region) &
                Attr('clusterArn').eq(cluster)
    )
    return(resp)

def ecs_getClusterArn(region, cluster):
    """ Given the ECS cluster name and the region, get the ECS ClusterARN. """
    client=boto3.client('ecs', region_name=region)
    response = client.describe_clusters(clusters=[cluster])

    logging.debug("ECS Cluster Details: %s", response)
    if len(response['clusters']) == 1:
        return (response['clusters'][0]['clusterArn'])
    else:
        return ''

def ec2_pricing(region, instance_type, tenancy, ostype):
    """
    Query AWS Pricing APIs to find cost of EC2 instance in the region.
    Given the paramters we use at input, we should get a UNIQUE result.
    TODO: In the current version, we only consider OnDemand price. If
    we start considering actual cost, we need to consider input from 
    CUR on an hourly basis.
    """
    svc_code = 'AmazonEC2'
    client = boto3.client('pricing', region_name="us-east-1")
    response = client.get_products(ServiceCode=svc_code,
        Filters = [
            {'Type' :'TERM_MATCH', 'Field':'location',          'Value':region},
            {'Type' :'TERM_MATCH', 'Field': 'servicecode',      'Value': svc_code},
            {'Type' :'TERM_MATCH', 'Field': 'preInstalledSw',   'Value': 'NA'},
            {'Type' :'TERM_MATCH', 'Field': 'tenancy',          'Value': tenancy},
            {'Type' :'TERM_MATCH', 'Field':'instanceType',      'Value':instance_type},
            {'Type' :'TERM_MATCH', 'Field': 'operatingSystem',  'Value': ostype}
        ],
        MaxResults=100
    )

    ret_list = []
    if 'PriceList' in response:
        for iter in response['PriceList']:
            ret_dict = {}
            mydict = ast.literal_eval(iter)
            ret_dict['memory'] = mydict['product']['attributes']['memory']
            ret_dict['vcpu'] = mydict['product']['attributes']['vcpu']
            ret_dict['instanceType'] = mydict['product']['attributes']['instanceType']
            ret_dict['operatingSystem'] = mydict['product']['attributes']['operatingSystem']
            ret_dict['normalizationSizeFactor'] = mydict['product']['attributes']['normalizationSizeFactor']

            mydict_terms = mydict['terms']['OnDemand'][ list( mydict['terms']['OnDemand'].keys() )[0]]
            ret_dict['unit'] = mydict_terms['priceDimensions'][list( mydict_terms['priceDimensions'].keys() )[0]]['unit']
            ret_dict['pricePerUnit'] = mydict_terms['priceDimensions'][list( mydict_terms['priceDimensions'].keys() )[0]]['pricePerUnit']
            ret_list.append(ret_dict)
    
    ec2_cpu  = float( ret_list[0]['vcpu'] )
    ec2_mem  = float( re.findall("[+-]?\d+\.?\d*", ret_list[0]['memory'])[0] )
    ec2_cost = float( ret_list[0]['pricePerUnit']['USD'] )
    return(ec2_cpu, ec2_mem, ec2_cost)

def ecs_pricing(region):
    """
    Get Fargate Pricing in the region.
    """
    svc_code = 'AmazonECS'
    client = boto3.client('pricing', region_name="us-east-1")
    response = client.get_products(ServiceCode=svc_code, 
        Filters = [
            {'Type' :'TERM_MATCH', 'Field':'location',          'Value':region},
            {'Type' :'TERM_MATCH', 'Field': 'servicecode',      'Value': svc_code},
        ],
        MaxResults=100
    )

    cpu_cost = 0.0
    mem_cost = 0.0

    if 'PriceList' in response:
        for iter in response['PriceList']:
            mydict = ast.literal_eval(iter)
            mydict_terms = mydict['terms']['OnDemand'][list( mydict['terms']['OnDemand'].keys() )[0]]
            mydict_price_dim = mydict_terms['priceDimensions'][list( mydict_terms['priceDimensions'].keys() )[0]]
            if mydict_price_dim['description'].find('CPU') > -1:
                cpu_cost = mydict_price_dim['pricePerUnit']['USD']
            if mydict_price_dim['description'].find('Memory') > -1:
                mem_cost = mydict_price_dim['pricePerUnit']['USD']

    return(cpu_cost, mem_cost)

def get_datetime_start_end(now, month, days, hours):

    logging.debug('In get_datetime_start_end(). month = %s, days = %s, hours = %s', month, days, hours)
    meter_end = now

    if month:
        # Will accept MM/YY and MM/YYYY format as input.
        regex = r"(?<![/\d])(?:0\d|[1][012])/(?:19|20)?\d{2}(?![/\d])"
        r = re.match(regex, month)
        if not r:
            print("Month provided doesn't look valid: %s" % (month))
            sys.exit(1)
        [m,y] = r.group().split('/')
        iy = 2000 + int(y) if int(y) <= 99 else int(y)
        im = int(m)

        meter_start = datetime.datetime(iy, im, 1, 0, 0, 0, 0, tzinfo=tzutc())
        meter_end = meter_start + relativedelta(months=1)

    if days:
        # Last N days = datetime(now) - timedelta (days = N)
        # Last N days could also be last N compelted days.
        # We use the former approach.
        if not days.isdigit():
            print("Duration provided is not a integer: %s" % (days))
            sys.exit(1)
        meter_start = meter_end - datetime.timedelta(days = int(days))
    if hours:
        if not hours.isdigit():
            print("Duration provided is not a integer" % (hours))
            sys.exit(1)
        meter_start = meter_end - datetime.timedelta(hours = int(hours))

    return (meter_start, meter_end)

def duration(startedAt, stoppedAt, startMeter, stopMeter, runTime, now):
    """
    Get the duration for which the task's cost needs to be calculated.
    This will vary depending on the CLI's input parameter (task lifetime,
    particular month, last N days etc.) and how long the task has run.
    """
    mRunTime = 0.0
    task_start = datetime.datetime.strptime(startedAt, '%Y-%m-%dT%H:%M:%S.%fZ')
    task_start = task_start.replace(tzinfo=datetime.timezone.utc)

    if (stoppedAt == 'STILL-RUNNING'):
        task_stop = now
    else:
        task_stop = datetime.datetime.strptime(stoppedAt, '%Y-%m-%dT%H:%M:%S.%fZ')
        task_stop = task_stop.replace(tzinfo=datetime.timezone.utc)

    # Return the complete task lifetime in seconds if metering duration is not provided at input.
    if not startMeter or not stopMeter:
        mRunTime = round ( (task_stop - task_start).total_seconds() )
        logging.debug('In duration (task lifetime): mRunTime=%f',  mRunTime)
        return(mRunTime)

    # Task runtime:              |------------|
    # Metering duration: |----|     or            |----|
    if (task_start >= stopMeter) or (task_stop <= startMeter): 
        mRunTime = 0.0
        logging.debug('In duration (meter duration different OOB): mRunTime=%f',  mRunTime)
        return(mRunTime)

    # Remaining scenarios:
    #
    # Task runtime:                |-------------|
    # Metering duration:   |----------|  or   |------|
    # Calculated duration:         |--|  or   |--|
    #
    # Task runtime:                |-------------|
    # Metering duration:              |-------|
    # Calculated duration:            |-------|
    #
    # Task runtime:                |-------------|
    # Metering duration:   |-------------------------|
    # Calculated duration:         |-------------|
    #

    calc_start = startMeter if (startMeter >= task_start) else task_start
    calc_stop = task_stop if (stopMeter >= task_stop) else stopMeter

    mRunTime = round ( (calc_stop - calc_start).total_seconds() )
    logging.debug('In duration(), mRunTime = %f', mRunTime)
    return(mRunTime)

def ec2_cpu2mem_weights(mem, cpu):
    # Depending on the type of instance, we can make split cost beteen CPU and memory
    # disproportionately.
    global cpu2mem_weight
    return (cpu2mem_weight)

def cost_of_ec2task(region, cpu, memory, ostype, instanceType, runTime):
    """
    Get Cost in USD to run a ECS task where launchMode==EC2.
    The AWS Pricing API returns all costs in hours. runTime is in seconds.
    """
    global pricing_dict
    global region_table

    pricing_key = '_'.join(['ec2',region, instanceType, ostype]) 
    if pricing_key not in pricing_dict:
        # Workaround for DUBLIN, Shared Tenancy and Linux
        (ec2_cpu, ec2_mem, ec2_cost) = ec2_pricing(region_table[region], instanceType, 'Shared', 'Linux')
        pricing_dict[pricing_key]={}
        pricing_dict[pricing_key]['cpu'] = ec2_cpu      # Number of CPUs on the EC2 instance
        pricing_dict[pricing_key]['memory'] = ec2_mem   # GiB of memory on the EC2 instance
        pricing_dict[pricing_key]['cost'] = ec2_cost    # Cost of EC2 instance (On-demand)

    # Corner case: When no CPU is assigned to a ECS Task, cpushares = 0
    # Workaround: Assume a minimum cpushare, say 128 or 256 (0.25 vcpu is the minimum on Fargate).
    if cpu == '0':
        cpu = '128'

    # Split EC2 cost bewtween memory and weights
    ec2_cpu2mem = ec2_cpu2mem_weights(pricing_dict[pricing_key]['memory'], pricing_dict[pricing_key]['cpu'])
    cpu_charges = ( (float(cpu)) / 1024.0 / pricing_dict[pricing_key]['cpu']) * ( float(pricing_dict[pricing_key]['cost']) * ec2_cpu2mem ) * (runTime/60.0/60.0)
    mem_charges = ( (float(memory)) / 1024.0 / pricing_dict[pricing_key]['memory'] ) * ( float(pricing_dict[pricing_key]['cost']) * (1.0 - ec2_cpu2mem) ) * (runTime/60.0/60.0)

    logging.debug('In cost_of_ec2task: mem_charges=%f, cpu_charges=%f',  mem_charges, cpu_charges)
    return(mem_charges, cpu_charges)

def cost_of_fgtask(region, cpu, memory, ostype, runTime):
    global pricing_dict
    global region_table

    pricing_key = 'fargate_' + region
    if pricing_key not in pricing_dict:
        # First time. Updating Dictionary
        # Workarond - for DUBLIN (cpu_cost, mem_cost) = ecs_pricing(region)
        (cpu_cost, mem_cost) = ecs_pricing(region_table[region])
        pricing_dict[pricing_key]={}
        pricing_dict[pricing_key]['cpu'] = cpu_cost
        pricing_dict[pricing_key]['memory'] = mem_cost

    mem_charges = ( (float(memory)) / 1024.0 ) * float(pricing_dict[pricing_key]['memory']) * (runTime/60.0/60.0)
    cpu_charges = ( (float(cpu)) / 1024.0 )    * float(pricing_dict[pricing_key]['cpu'])    * (runTime/60.0/60.0)

    logging.debug('In cost_of_fgtask: mem_charges=%f, cpu_charges=%f',  mem_charges, cpu_charges)
    return(mem_charges, cpu_charges)

def cost_of_service(tasks, meter_start, meter_end, now):
    fargate_service_cpu_cost = 0.0
    fargate_service_mem_cost = 0.0
    ec2_service_cpu_cost = 0.0
    ec2_service_mem_cost = 0.0

    if 'Items' in tasks:
        for task in tasks['Items']:
            runTime = duration(task['startedAt'], task['stoppedAt'], meter_start, meter_end, float(task['runTime']), now)

            logging.debug("In cost_of_service: runTime = %f seconds", runTime)
            if task['launchType'] == 'FARGATE':
                fargate_mem_charges,fargate_cpu_charges = cost_of_fgtask(task['region'], task['cpu'], task['memory'], task['osType'], runTime)
                fargate_service_mem_cost += fargate_mem_charges
                fargate_service_cpu_cost += fargate_cpu_charges
            else:
                # EC2 Task
                ec2_mem_charges, ec2_cpu_charges = cost_of_ec2task(task['region'], task['cpu'], task['memory'], task['osType'], task['instanceType'], runTime)
                ec2_service_mem_cost += ec2_mem_charges
                ec2_service_cpu_cost += ec2_cpu_charges

    return(fargate_service_cpu_cost, fargate_service_mem_cost, ec2_service_mem_cost, ec2_service_cpu_cost)

if __name__ == "__main__":

    parser = ArgumentParser()
    parser.add_argument('--region',  '-r', required=True, help="AWS Region in which Amazon ECS service is running.")
    parser.add_argument('--cluster', '-c', required=True, help="ClusterARN in which Amazon ECS service is running.")
    parser.add_argument('--service', '-s', required=True, help="Name of the AWS ECS service for which cost has to be calculated.")
    parser.add_argument('--weight',  '-w', default=0.5, required=False, help="Floating point value that defines CPU:Memory Cost Ratio to be used for dividing EC2 pricing")
    parser.add_argument("-v", "--verbose", action="store_true")

    period = parser.add_mutually_exclusive_group(required=False)
    period.add_argument('--month', '-M', help='Show charges for a service for a particular month')
    period.add_argument('--days',  '-D', help='Show charges for a service for last N days')
    period.add_argument('--hours',  '-H', help='Show charges for a service for last N hours')

    cli_args = parser.parse_args()
    region = cli_args.region
    service = cli_args.service

    metered_results = True if (cli_args.month or cli_args.days or cli_args.hours) else False

    # Load region table. We need this to get a mapping of region_name (for e.g. eu-west-1) to
    # the region_friendly_name (for e.g. 'EU (Ireland)'). Currently, there is no programmatic way
    # of doing this. We need this to use the AWS Pricing APIs.
    try:
        with open('region_table.json', encoding='utf-8') as f:
            region_table = json.load(f)
            if region not in region_table.keys():
                raise
    except:
        print("Unexpected error: Unable to read region_table.json or region (%s) not found" % (region))
        sys.exit(1)

    cpu2mem_weight = float(cli_args.weight)

    if cli_args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    clustername = cli_args.cluster
    cluster = ecs_getClusterArn(region, clustername)
    if not cluster:
        logging.error("Cluster : %s Missing", clustername)
        sys.exit(1)

    now = datetime.datetime.now(tz=tzutc())

    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table("ECSTaskStatus")

    tasks = get(table, region, cluster, service)

    if metered_results:
        (meter_start, meter_end) = get_datetime_start_end(now, cli_args.month, cli_args.days, cli_args.hours)
        (fg_cpu, fg_mem, ec2_mem, ec2_cpu) = cost_of_service(tasks, meter_start, meter_end, now)
    else: 
        (fg_cpu, fg_mem, ec2_mem, ec2_cpu) = cost_of_service(tasks, 0, 0, now)


    logging.debug("Main: fg_cpu=%f, fg_mem=%f, ec2_mem=%f, ec2_cpu=%f", fg_cpu, fg_mem, ec2_mem, ec2_cpu)

    print("#####################################################################")
    print("#")
    print("# ECS Region  : %s, ECS Service Name: %s" % (region, service) )
    print("# ECS Cluster : %s" % (cluster))
    print("#")

    if metered_results:
        if cli_args.month:
            print("# Cost calculated for month %s" % (cli_args.month) )
        else:
            print("# Cost calculated for last %s %s" % (cli_args.days if cli_args.days else cli_args.hours, "days" if cli_args.days else "hours") )

    print("#")

    if ec2_mem or ec2_cpu:
        print("# Amazon ECS Service Cost           : %.6f USD" % (ec2_mem+ec2_cpu) )
        print("#         (Launch Type : EC2)")
        print("#         EC2 vCPU Usage Cost       : %.6f USD" % (ec2_cpu) )
        print("#         EC2 Memory Usage Cost     : %.6f USD" % (ec2_mem) )
    if fg_cpu or fg_mem:
        print("# Amazon ECS Service Cost           : %.6f USD" % (fg_mem+fg_cpu) )
        print("#         (Launch Type : FARGATE)")
        print("#         Fargate vCPU Usage Cost   : %.6f USD" % (fg_cpu) )
        print("#         Fargate Memory Usage Cost : %.6f USD" % (fg_mem) )
        print("#")

    if not (fg_cpu or fg_mem or ec2_mem or ec2_cpu):
        print("# Service Cost: 0 USD. Service not running in specified duration!")
        print("#")

    print("#####################################################################")

    sys.exit(0)