#!/usr/bin/env python

"""
Program to generate monthly summaries for a specific set of interface, 
aggregated on the device/interface/endpoint.  These aggregations are 
derived from the daily rollup data and are written to the timestamp of
the first second of the month being aggregated.

Usage and args:

-U : the url to the REST interface of the form: http://host, http://host:port,
etc.  This is required - defaults to http://localhost.

-a or -i : specifies a search patter for either an interface alias (-a) or 
an interface name/ifname (-i).  One or the other is required.  This is used
to perform a django filtering search to generate a group of interfaces. A 
django '__contains' (ifName__contains, etc) query on the interfaces.

-e : specifies an endpoint type (in, out, error/in, discard/out, etc) to 
retrieve and aggregate data for.  This option may be specified more than 
once (-e in -e out) to include more than one endpoint alias in the generated
aggregations.  This is "optional" as the query library default to just 'in.'

-m : the month that the aggregations are to be generated for.  When supplied,
the arg is in the form YYYY-MM and the specified month will be aggregated. 
If not supplied it will look at the current time, and perform an aggregation
on the previous month.

-P : a boolean flag indicated that the generated aggregations are POSTed 
and stored in the cassandra backend.  This does not need to be specified 
if the user just wants to generate an look at the generated objects and 
values (generally in conjunction with the -v flag) for debugging or 
edification.

-u and -k : user name and api key string - these will need to be specified 
if the -P flag is invoked to store the generated aggregations as the 
/timeseries POSTs are protected with api key authorization.

-v and -vv : produce increasingly noisy output.

Aggregation: the filtering search is performed to pull back the data 
over the month from the matched interface rollups, the values from each 
device/interface/endpoint are summed togther and stored.

And additional aggregation is performed where all of the aggregates sums 
for each endpoint type are summed together to produce a monthly grand
total for in, out, discard/in, etc.

Storage: the aggregated values are writtent to the "raw_data" column 
family in the esmond cassandra backend.  The keys are of the following 
format(s):

monthly:TotalTrafficMe0.0:star-asw1:me0.0:out:86400000:2013

or:

monthly:TotalTrafficMe0.0:in:86400000:2013

The first key type is the more numerous format.  That will contain the 
monthly aggregations for a specific device/interface/endpoint.  The second
key holds the montly grand totals (for the 'in' endpoints in this case).

For the former type:  

The first 'summary' segment is just a 'namespace' that prefixes all of the 
keys of the summary data (as opposed to the 'snmp' namespace that) the 
live data is written to.  The second segment (TotalTrafficIntercloud in 
this case) is the 'summary name' that is mapped to a given query (see next
section).  The next three segments are the device name, interface name and 
endpoint alias respectively.  The 86400000 frequency (86400 sec in ms) is
used because 1) there is no seconds value for a month and 2) the aggregates
are derived from 86400 second rollups.  The trailing year segment is 
automatically generated by the cassandra.py module logic.

For the latter/grand total type:

These keys are very much like former type, but there is no device or 
interface segment.  

The way the data are written, all of the aggregations are idempotent, so 
running the same search over the same time period will yield the same 
results being stored (unless the base rate data has been updated in the 
meantime).

All of the values in a given row are the timestamp of the first second of 
the month being aggregated, and the summed values as appropriate for the 
device/interface/endpoint or grand totals.  This means that a row for 
given year of monthly summaries will only contain 12 columns.  This isn't
the most optimal use of the 'cassandra wide-row model,' but is consistent
with the way that we write all the other data.

Summary name: the summary name segment is an operator-specified string that 
maps to a specific search criteria.  It is important that this value not be 
overloaded because if it is, aggs from one query will over-write previously
generated aggregations.  To better illustrate how this mapping works, and 
to show the way that the users can manage this, all of the these have 
been put in a configuration file (see: interface_summary.conf).  Example
entries:

# Mappings for ifname filters
[ifName__contains]
me0.0: TotalTrafficMe0.0

# Mappings for ifalias filters
[ifAlias__contains]
intercloud: TotalTrafficIntercloud

The [sections] correspond to the filtering query that is being performed, 
the key of a given entry is the actual search criteria used in the filter, 
and the value of a given entry is the summary name that is used when 
formulating the row key.  So this row key:

monthly:TotalTrafficMe0.0:aofa-asw1:me0.0:in:86400000:2013

contains the aggregations for one of the device/iface/endpoints ('in' in 
this case) returned by the query "ifName__contains=me0.0".

Retrieval:

The /timeseries rest namespace exposes the functionaly to retrieve arbitrary
data from the backend given the correctly formulated URI.  These aggregates 
can be retrieved with queries of the following (general) form:

GET /v1/timeseries/RawData/monthly/TotalTrafficMe0.0/aofa-asw1/me0.0/in/86400000?begin=1377993600000&end=1377993600000

for the device/interface/endpoint aggs or 

GET /v1/timeseries/RawData/monthly/TotalTrafficMe0.0/out/86400000?begin=1377993600000&end=1377993600000

for the grand total aggs.  The appropriate begin and end times are expressed
in ms.  Running this code with -v will verify the writes and show 
how the paths/args can be constructed to use the GetRawData() class at 
the end of the program.

"""
import datetime
import os
import os.path
import requests
import sys
import time

from optparse import OptionParser

from esmond.api.client.snmp import ApiConnect, ApiFilters
from esmond.api.client.timeseries import PostRawData, GetRawData
from esmond.api.client.util import MONTHLY_NS, get_summary_name, \
    aggregate_to_device_interface_endpoint, lastmonth, \
    get_month_start_and_end, iterate_device_interface_endpoint

# Chosen because there isn't a seconds value for a month, so using
# the aggregation value for a day because the monthly summaries
# are derived from daily rollups.
AGG_FREQUENCY = 86400

def main():    
    usage = '%prog [ -U rest url (required) | -i ifName pattern | -a alias pattern | -e endpoint -e endpoint (multiple ok) ]'
    parser = OptionParser(usage=usage)
    parser.add_option('-U', '--url', metavar='ESMOND_REST_URL',
            type='string', dest='api_url', 
            help='URL for the REST API (default=%default) - required.',
            default='http://localhost')
    parser.add_option('-i', '--ifname', metavar='IFNAME',
            type='string', dest='ifname_pattern', 
            help='Pattern to apply to interface ifname search.')
    parser.add_option('-a', '--alias', metavar='ALIAS',
            type='string', dest='alias_pattern', 
            help='Pattern to apply to interface alias search.')
    parser.add_option('-e', '--endpoint', metavar='ENDPOINT',
            dest='endpoint', action='append', default=[],
            help='Endpoint type to query (required) - can specify more than one.')
    parser.add_option('-m', '--month', metavar='MONTH',
            type='string', dest='month', default='',
            help='Specify month in YYYY-MM format.')
    parser.add_option('-v', '--verbose',
                dest='verbose', action='count', default=False,
                help='Verbose output - -v, -vv, etc.')
    parser.add_option('-P', '--post',
            dest='post', action='store_true', default=False,
            help='Switch to actually post data to the backend - otherwise it will just query and give output.')
    parser.add_option('-u', '--user', metavar='USER',
            type='string', dest='user', default='',
            help='POST interface username.')
    parser.add_option('-k', '--key', metavar='API_KEY',
            type='string', dest='key', default='',
            help='API key for POST operation.')
    options, args = parser.parse_args()

    if not options.month:
        print 'No -m arg, defaulting to last month'
        now = datetime.datetime.utcnow()
        start_year, start_month = lastmonth((now.year,now.month))
        start_point = datetime.datetime.strptime('{0}-{1}'.format(start_year, start_month),
            '%Y-%m')
    else:
        print 'Parsing -m input {0}'.format(options.month)
        try:
            start_point = datetime.datetime.strptime(options.month, '%Y-%m')
        except ValueError:
            print 'Unable to parse -m arg {0} - expecting YYYY-MM format'.format(options.month)
            return -1

    print 'Generating monthly summary starting on: {0}'.format(start_point)

    start, end = get_month_start_and_end(start_point)

    if options.verbose: print 'Scanning from {0} to {1}'.format(
        datetime.datetime.utcfromtimestamp(start), 
        datetime.datetime.utcfromtimestamp(end)
    )
    
    filters = ApiFilters()

    filters.verbose = options.verbose
    filters.endpoint = options.endpoint
    filters.agg = AGG_FREQUENCY
    filters.cf = 'raw'

    filters.begin_time = start
    filters.end_time = end

    if not options.ifname_pattern and not options.alias_pattern:
        # Don't grab *everthing*.
        print 'Specify an ifname or alias filter option.'
        parser.print_help()
        return -1
    elif options.ifname_pattern and options.alias_pattern:
        print 'Specify only one filter option.'
        parser.print_help()
        return -1
    else:
        if options.ifname_pattern:
            interface_filters = { 'ifName__contains': options.ifname_pattern }
        elif options.alias_pattern:
            interface_filters = { 'ifAlias__contains': options.alias_pattern }

    conn = ApiConnect(options.api_url, filters, options.user, options.key)

    data = conn.get_interface_bulk_data(**interface_filters)

    print data

    aggs = aggregate_to_device_interface_endpoint(data, options.verbose)

    # Generate the grand total
    total_aggs = {}

    for d, i, endpoint, val in iterate_device_interface_endpoint(aggs):
        if not total_aggs.has_key(endpoint): total_aggs[endpoint] = 0
        total_aggs[endpoint] += val

    if options.verbose: print 'Grand total:', total_aggs

    # Roll everything up before posting
    summary_name = get_summary_name(interface_filters)

    post_data = {}

    for device, interface, endpoint, val in iterate_device_interface_endpoint(aggs):
        path = (MONTHLY_NS, summary_name, device, interface, endpoint)
        payload = { 'ts': start*1000, 'val': val }
        if options.verbose > 1: print path, '\n\t', payload
        post_data[path] = payload                

    for endpoint, val in total_aggs.items():
        path = (MONTHLY_NS, summary_name, endpoint)
        payload = { 'ts': start*1000, 'val': val }
        if options.verbose > 1: print path, '\n\t', payload
        post_data[path] = payload

    if not options.post:
        print 'Not posting (use -P flag to write to backend).'
        return

    if not options.user or not options.key:
        print 'user and key args must be supplied to POST summary data.'
        return

    for path, payload in post_data.items():
        args = {
            'api_url': options.api_url, 
            'path': list(path), 
            'freq': AGG_FREQUENCY*1000
        }
        
        p = PostRawData(username=options.user, api_key=options.key, **args)
        p.add_to_payload(payload)
        p.send_data()

        if options.verbose:
            print 'verifying write for', path
            p = { 'begin': start*1000, 'end': start*1000 }
            g = GetRawData(params=p, **args)
            result = g.get_data()
            print result, '\n\t', result.data[0]

    return

if __name__ == '__main__':
    main()
