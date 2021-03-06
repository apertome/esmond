#!/usr/bin/env python
# encoding: utf-8
"""
Cassandra DB interface calls and data encapsulation objects.

esmond schema in json-like notation:

// regular col family
"raw_data" : {
    "snmp:router_a:FastPollHC:ifHCInOctets:xe-0_2_0:30000:2012" : {
        "1343955624" :   // long column name
        "16150333739148" // UTF-8 containing JSON for values.
    }
}

// supercolumn
"base_rates" : {
    "snmp:router_a:FastPollHC:ifHCInOctets:xe-0_2_0:30000:2012" : {
        "1343955600" : {     // long column name.
            "val": "123",    // string key, counter type value.
            "is_valid" : "2" // zero or positive non-zero.
        }
    }
}

// supercolumn
"rate_aggregations" : {
    "snmp:router_a:FastPollHC:ifHCInOctets:xe-0_2_0:3600000:2012" : {
        "1343955600" : {   // long column name.
            "val": "1234", // string key, counter type.
            "30": "38"     // key of the 'non-val' column is freq of the base rate.
        }                  // the value of said is the count used in the average.
    }
}

// supercolumn
"stat_aggregations" : {
    "snmp:router_a:FastPollHC:ifHCInOctets:xe-0_2_0:86400000:2012" : {
        "1343955600" : { // long column name.
            "min": "0",  // string keys, long types.
            "max": "484140" 
        }
    }
}
"""
# Standard
import ast
import calendar
import datetime
import json
import logging
import os
import pprint
import sys
import time
from collections import OrderedDict

from esmond.util import get_logger

# Third party
from pycassa import PycassaLogger
from pycassa.pool import ConnectionPool, AllServersUnavailable, MaximumRetryException
from pycassa.columnfamily import ColumnFamily, NotFoundException
from pycassa.system_manager import *

from thrift.transport.TTransport import TTransportException

SEEK_BACK_THRESHOLD = 2592000000 # 30 days in ms
KEY_DELIMITER = ":"
AGG_TYPES = ['average', 'min', 'max', 'raw']

class CassandraException(Exception):
    """Common base"""
    pass

class ConnectionException(CassandraException):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)
        
class CASSANDRA_DB(object):
    
    raw_cf = 'raw_data'
    rate_cf = 'base_rates'
    agg_cf = 'rate_aggregations'
    stat_cf = 'stat_aggregations'
    
    _queue_size = 200
    
    def __init__(self, config, qname=None):
        """
        Class contains all the relevent cassandra logic.  This includes:
        
        * schema creation,
        * connection information/pooling, 
        * generating the metadata cache of last val/ts information,
        * store data/update the rate/aggregaion bins,
        * and execute queries to return data to the REST interface.
        """
        
        # Configure logging - if a qname has been passed in, hook
        # into the persister logger, if not, toss together some fast
        # console output for devel/testing.
        if qname:
            self.log = get_logger("espersistd.%s.cass_db" % qname)
        else:
            self.log = logging.getLogger('cassandra_db')
            self.log.setLevel(logging.DEBUG)
            format = logging.Formatter('%(name)s [%(levelname)s] %(message)s')
            handle = logging.StreamHandler()
            handle.setFormatter(format)
            self.log.addHandler(handle)
        
        # Add pycassa driver logging to existing logger.
        plog = PycassaLogger()
        plog.set_logger_name('%s.pycassa' % self.log.name)
        # Debug level is far too noisy, so just hardcode the pycassa 
        # logger to info level.
        plog.set_logger_level('info')

        # Connect to cassandra with SystemManager, do a schema check 
        # and set up schema components if need be.
        if ast.literal_eval(os.environ.get('ESMOND_UNIT_TESTS', 'False')):
            print '*** Using test keyspace'
            self.keyspace = 'test_{0}'.format(config.cassandra_keyspace)
        else:
            self.keyspace = config.cassandra_keyspace

        try:
            sysman = SystemManager(config.cassandra_servers[0])                              
        except TTransportException, e:
            raise ConnectionException("System Manager can't connect to Cassandra "
                "at %s - %s" % (config.cassandra_servers[0], e))
        
        # Blow everything away if we're testing - be aware of this and use
        # with care.  Currently just being explictly set in test harness
        # code but no longer set as a config file option since there could
        # be unfortunate side effects.
        if config.db_clear_on_testing:
            self.log.info('Dropping keyspace %s' % self.keyspace)
            if self.keyspace in sysman.list_keyspaces():
                sysman.drop_keyspace(self.keyspace)
                time.sleep(3)
        # Create keyspace
        
        _schema_modified = False # Track if schema components are created.
        
        if not self.keyspace in sysman.list_keyspaces():
            _schema_modified = True
            self.log.info('Creating keyspace %s' % self.keyspace)
            sysman.create_keyspace(self.keyspace, SIMPLE_STRATEGY, 
                {'replication_factor': str(config.cassandra_replicas)})
            time.sleep(3)
        # Create column families if they don't already exist.
        # If a new column family is added, make sure to set 
        # _schema_modified = True so it will be propigated.
        self.log.info('Checking/creating column families')
        # Raw Data CF
        if not sysman.get_keyspace_column_families(self.keyspace).has_key(self.raw_cf):
            _schema_modified = True
            sysman.create_column_family(self.keyspace, self.raw_cf, super=False, 
                    comparator_type=LONG_TYPE, 
                    default_validation_class=UTF8_TYPE,
                    key_validation_class=UTF8_TYPE,
                    compaction_strategy='LeveledCompactionStrategy')
            self.log.info('Created CF: %s' % self.raw_cf)
        # Base Rate CF
        if not sysman.get_keyspace_column_families(self.keyspace).has_key(self.rate_cf):
            _schema_modified = True
            sysman.create_column_family(self.keyspace, self.rate_cf, super=True, 
                    comparator_type=LONG_TYPE, 
                    default_validation_class=COUNTER_COLUMN_TYPE,
                    key_validation_class=UTF8_TYPE,
                    compaction_strategy='LeveledCompactionStrategy')
            self.log.info('Created CF: %s' % self.rate_cf)
        # Rate aggregation CF
        if not sysman.get_keyspace_column_families(self.keyspace).has_key(self.agg_cf):
            _schema_modified = True
            sysman.create_column_family(self.keyspace, self.agg_cf, super=True, 
                    comparator_type=LONG_TYPE, 
                    default_validation_class=COUNTER_COLUMN_TYPE,
                    key_validation_class=UTF8_TYPE,
                    compaction_strategy='LeveledCompactionStrategy')
            self.log.info('Created CF: %s' % self.agg_cf)
        # Stat aggregation CF
        if not sysman.get_keyspace_column_families(self.keyspace).has_key(self.stat_cf):
            _schema_modified = True
            sysman.create_column_family(self.keyspace, self.stat_cf, super=True, 
                    comparator_type=LONG_TYPE, 
                    default_validation_class=LONG_TYPE,
                    key_validation_class=UTF8_TYPE,
                    compaction_strategy='LeveledCompactionStrategy')
            self.log.info('Created CF: %s' % self.stat_cf)
                    
        sysman.close()
        
        self.log.info('Schema check done')
        
        # If we just cleared the keyspace/data and there is more than
        # one server, pause to let schema propigate to the cluster machines.
        if _schema_modified == True:
            self.log.info("Waiting for schema to propagate...")
            time.sleep(10)
            self.log.info("Done")
                
        # Now, set up the ConnectionPool
        
        # Read auth information from config file and set up if need be.
        _creds = {}
        if config.cassandra_user and config.cassandra_pass:
            _creds['username'] = config.cassandra_user
            _creds['password'] = config.cassandra_pass
            self.log.debug('Connecting with username: %s' % (config.cassandra_user,))
        
        try:
            self.log.debug('Opening ConnectionPool')
            self.pool = ConnectionPool(self.keyspace, 
                server_list=config.cassandra_servers, 
                pool_size=10,
                max_overflow=5,
                max_retries=10,
                timeout=30,
                credentials=_creds)
        except AllServersUnavailable, e:
            raise ConnectionException("Couldn't connect to any Cassandra "
                    "at %s - %s" % (config.cassandra_servers, e))
                    
        self.log.info('Connected to %s' % config.cassandra_servers)
        
        # Define column family connections for the code to use.
        self.raw_data = ColumnFamily(self.pool, self.raw_cf).batch(self._queue_size)
        self.rates    = ColumnFamily(self.pool, self.rate_cf).batch(self._queue_size)
        self.aggs     = ColumnFamily(self.pool, self.agg_cf).batch(self._queue_size)
        self.stat_agg = ColumnFamily(self.pool, self.stat_cf).batch(self._queue_size)

        # Used when a cf needs to be selected on the fly.
        self.cf_map = {
            'raw': self.raw_data,
            'rate': self.rates,
            'aggs': self.aggs,
            'stat': self.stat_agg
        }
        
        # Timing - this turns the database call profiling code on and off.
        # This is not really meant to be used in production and generally 
        # just spits out statistics at the end of a run of test data.  Mostly
        # useful for timing specific database calls to aid in development.
        self.profiling = False
        if config.db_profile_on_testing and os.environ.get("ESMOND_TESTING", False):
            self.profiling = True
        self.stats = DatabaseMetrics(profiling=self.profiling)
        
        # Class members
        # Just the dict for the metadata cache.
        self.metadata_cache = {}
        self.aggregation_cache = {}
        
    def flush(self):
        """
        Calling this will explicity flush all the batches to the 
        server.  Generally only used in testing/dev scripts and not
        in production when the batches will be self-flushing.
        """
        self.log.debug('Flush called')
        self.raw_data.send()
        self.rates.send()
        self.aggs.send()
        self.stat_agg.send()
        
    def close(self):
        """
        Explicitly close the connection pool.
        """
        self.log.debug('Close/dispose called')
        self.pool.dispose()
        
    def set_raw_data(self, raw_data, ttl=None):
        """
        Called by the persister.  Writes the raw incoming data to the appropriate
        column family.  The optional TTL option is passed in self.raw_opts and 
        is set up in the constructor.
        
        The raw_data arg passes in is an instance of the RawData class defined
        in this module.
        """
        _kw = {}
        if ttl: 
            _kw['ttl'] = ttl

        t = time.time()
        # Standard column family update.
        self.raw_data.insert(raw_data.get_key(), 
            {raw_data.ts_to_jstime(): json.dumps(raw_data.val)}, **_kw)
        
        if self.profiling: self.stats.raw_insert(time.time() - t)
        
    def set_metadata(self, k, meta_d):
        """
        Just does a simple write to the dict being used as metadata.
        """
        self.metadata_cache[k] = meta_d.get_document()
        
    def get_metadata(self, raw_data):
        """
        Called by the persister to get the metadata - last value and timestamp -
        for a given measurement.  If a given value is not found (as in when the 
        program is initially started for example) it will look in the raw data
        as far back as SEEK_BACK_THRESHOLD to find the previous value.  If found,
        This is seeded to the cache and returned.  If not, this is presumed to be
        new, and the cache is seeded with the value that is passed in.
        
        The raw_data arg passes in is an instance of the RawData class defined
        in this module.
        
        The return value is a Metadata object, also defined in this module.
        """
        t = time.time()

        meta_d = None
        
        if not self.metadata_cache.has_key(raw_data.get_meta_key()):
            # Didn't find a value in the metadata cache.  First look
            # back through the raw data for SEEK_BACK_THRESHOLD seconds
            # to see if we can find the last processed value.
            ts_max = raw_data.ts_to_jstime() - 1 # -1ms to look at older vals
            ts_min = ts_max - SEEK_BACK_THRESHOLD
            ret = self.raw_data._column_family.multiget(
                    self._get_row_keys(raw_data.path, raw_data.freq,
                        ts_min, ts_max),
                    # Note: ts_max and ts_min appear to be reversed here - 
                    # that's because this is a reversed range query.
                    column_start=ts_max, column_finish=ts_min,
                    column_count=1, column_reversed=True)
                    
            if self.profiling: self.stats.meta_fetch((time.time() - t))
                    
            if ret:
                # A previous value was found in the raw data, so we can
                # seed/return that.
                key = ret.keys()[-1]
                ts = ret[key].keys()[0]
                val = json.loads(ret[key][ts])
                meta_d = Metadata(last_update=ts, last_val=val, min_ts=ts, 
                    freq=raw_data.freq, path=raw_data.path)
                self.log.debug('Metadata lookup from raw_data for: %s' %
                        (raw_data.get_meta_key()))
            else:
                # No previous value was found (or at least not one in the defined
                # time range) so seed/return the current value.
                meta_d = Metadata(last_update=raw_data.ts, last_val=raw_data.val,
                    min_ts=raw_data.ts, freq=raw_data.freq, path=raw_data.path)
                self.log.debug('Initializing metadata for: %s using %s' %
                        (raw_data.get_meta_key(), raw_data))
            self.set_metadata(raw_data.get_meta_key(), meta_d)
        else:
            meta_d = Metadata(**self.metadata_cache[raw_data.get_meta_key()])
        
        return meta_d
        
    def update_metadata(self, k, metadata):
        """
        Update the metadata cache with a recently updated value.  Called by the
        persister.
        
        The metadata arg is a Metadata object defined in this module.
        """
        t = time.time()
        for i in ['last_val', 'min_ts', 'last_update']:
            self.metadata_cache[k][i] = getattr(metadata, i)
        #self.stats.meta_update((time.time() - t))
    
    def update_rate_bin(self, ratebin):
        """
        Called by the persister.  This updates a base rate bin in the base 
        rate column family.  
        
        The ratebin arg is a BaseRateBin object defined in this module.
        """
        
        t = time.time()
        # A super column insert.  Both val and is_valid are counter types.
        try:
            self.rates.insert(ratebin.get_key(),
                {ratebin.ts_to_jstime(): {'val': ratebin.val, 'is_valid': ratebin.is_valid}})
        except MaximumRetryException:
            self.log.warn("update_rate_bin failed. MaximumRetryException")

        if self.profiling: self.stats.baserate_update((time.time() - t))
        
    def update_rate_aggregation(self, raw_data, agg_ts, freq):
        """
        Called by the persister to update the rate aggregation rollups.
        
        The args are a RawData object, the "compressed" aggregation timestamp
        and the frequency of the rollups in seconds.
        """
        
        t = time.time()
        
        agg = AggregationBin(
            ts=agg_ts, freq=freq, val=raw_data.val, base_freq=raw_data.freq, count=1,
            min=raw_data.val, max=raw_data.val, path=raw_data.path
        )
        
        # Super column update.  The base rate frequency is stored as the column
        # name key that is not 'val' - this will be used by the query interface
        # to generate the averages.  Both values are counter types.
        try:
            self.aggs.insert(agg.get_key(),
                {agg.ts_to_jstime(): {'val': agg.val, str(agg.base_freq): 1}})
        except MaximumRetryException:
            self.log.warn("update_rate_aggregation failed. MaximumRetryException")

        if self.profiling: self.stats.aggregation_update((time.time() - t))

    def get_agg_from_cache(self, agg, raw_data):
        """
        Manage aggregations using in-memory state similar to tracking
        the previous value when calculating the base rates.  Cache is a 
        multi-level dictionary that looks like:

        cache[row_key][timestamp_of_agg_bin] = {'min': 900 'max': .....}

        If a row key is not present in the cache, a lookup is done on 
        the stat aggregation column family to see if this is a restart
        situation.  If an entry is found for the agg timestamp/bin 
        that is being processed, the cache is seeded with those values.  
        This is the only time the database is read.

        If no entry is found, then the cache is seeded with the initial
        incoming values (new interface added, etc.)

        Subsequent lookups will use the state in the cache.  When 
        a new aggregation bin is started, the entry for the row 
        key is 'zeroed out':

        cache[row_key] = dict()

        to remove entries for previous aggregation bins avoiding
        memory leaks.
        """

        ret = None

        if not self.aggregation_cache.get(agg.get_key(), None):
            self.aggregation_cache[agg.get_key()] = dict()
            # there is no row key for this aggregation so to 
            # an initial lookup to see if this is a restart and seed 
            # the cache from the currently requested aggregation.
            # this read will only happen once per aggregation row
            # after startup or when seeing a new interface, etc.
            try:
                lookup = self.stat_agg._column_family.get(agg.get_key(), 
                            super_column=agg.ts_to_jstime())
                self.aggregation_cache[agg.get_key()][agg.ts_to_jstime()] = dict(lookup)
            except NotFoundException:
                pass


        if not self.aggregation_cache[agg.get_key()].get(agg.ts_to_jstime(), None):
            # a new bin is being started, so blow away previous 
            # timestamped key for this row and start again so as to 
            # not be leaking memory.
            self.aggregation_cache[agg.get_key()] = dict()
            # and update with the new aggregation bin values.  do not
            # return a value so update_stat_aggregations will do the 
            # initial insert().
            self.aggregation_cache[agg.get_key()][agg.ts_to_jstime()] = \
                {'min': agg.val, 'max': agg.val, 'min_ts': raw_data.ts_to_jstime(), 'max_ts': raw_data.ts_to_jstime()}
        else:
            ret = self.aggregation_cache[agg.get_key()].get(agg.ts_to_jstime())

        return ret

    def update_agg_cache(self, agg, raw_data, minmax):
        """Helper function to update agg cache when a new min or max happens."""
        assert minmax in ['min', 'max']

        self.aggregation_cache[agg.get_key()][agg.ts_to_jstime()]['{0}'.format(minmax)] = agg.val
        self.aggregation_cache[agg.get_key()][agg.ts_to_jstime()]['{0}_ts'.format(minmax)] = raw_data.ts_to_jstime()

        
    def update_stat_aggregation(self, raw_data, agg_ts, freq):
        """
        Called by the persister to update the stat aggregations (ie: min/max).
        
        Unlike the other update code, this has to read from the appropriate bin 
        to see if the min or max needs to be updated.  The update is done if 
        need be, and the updated boolean is set to true and returned to the
        calling code to flush the batch if need be.  Done that way to flush 
        more than one batch update rather than doing it each time.
        
        The args are a RawData object, the "compressed" aggregation timestamp
        and the frequency of the rollups in seconds.
        """
        
        updated = False
        
        # Create the AggBin object.
        agg = AggregationBin(
            ts=agg_ts, freq=freq, val=raw_data.val, base_freq=raw_data.freq, count=1,
            min=raw_data.val, max=raw_data.val, path=raw_data.path
        )
        
        t = time.time()

        ret = self.get_agg_from_cache(agg, raw_data)
        
        if self.profiling: self.stats.stat_fetch((time.time() - t))
        
        t = time.time()
        
        if not ret:
            # Bin does not exist, so initialize min and max with the same val.
            # self.stat_agg.insert(agg.get_key(),
            #     {agg.ts_to_jstime(): {'min': agg.val, 'max': agg.val}})
            self.stat_agg.insert(agg.get_key(),
                {agg.ts_to_jstime(): {'min': agg.val, 'max': agg.val, 'min_ts': raw_data.ts_to_jstime(), 'max_ts': raw_data.ts_to_jstime()}})
            updated = True
        elif agg.val > ret['max']:
            # Update max.
            self.update_agg_cache(agg, raw_data, 'max')
            self.stat_agg.insert(agg.get_key(),
                {agg.ts_to_jstime(): {'max': agg.val, 'max_ts': raw_data.ts_to_jstime()}})
            updated = True
        elif agg.val < ret['min']:
            self.update_agg_cache(agg, raw_data, 'min')
            # Update min.
            self.stat_agg.insert(agg.get_key(),
                {agg.ts_to_jstime(): {'min': agg.val, 'min_ts': raw_data.ts_to_jstime()}})
            updated = True
        else:
            pass
        
        if self.profiling: self.stats.stat_update((time.time() - t))
        
        return updated
        
    def _get_row_keys(self, path, freq, ts_min, ts_max):
        """
        Utility function used by the query interface.
        
        Given these values and the starting/stopping timestamp, return a
        list of row keys (ie: more than one if the query spans years) to
        be used as the first argument to a multiget cassandra query.
        """
       
        year_start = datetime.datetime.utcfromtimestamp(float(ts_min)/1000.0).year
        year_finish = datetime.datetime.utcfromtimestamp(float(ts_max)/1000.0).year
        
        key_range = []
        
        if year_start != year_finish:
            for year in range(year_start, year_finish+1):
                key_range.append(get_rowkey(path, freq=freq, year=year))
        else:
            key_range.append(get_rowkey(path, freq=freq, year=year_start))
        return key_range

    def check_for_valid_keys(self, path=None, freq=None, 
            ts_min=None, ts_max=None, col_fam='rate'):
        """
        Utility function used to discrete key/set of keys exists.  Used 
        by api/etc see if an invalid key is the reason no data is returned.
        """

        found = False

        keys = self._get_row_keys(path,freq,ts_min,ts_max)

        for key in keys:
            try:
                self.cf_map[col_fam]._column_family.get(key, column_count=1)
            except NotFoundException:
                # Key was not found.
                pass
            else:
                # Key was found so mark boolean as good - revisit?
                found = True

        return found
        
    def query_baserate_timerange(self, path=None, freq=None, 
            ts_min=None, ts_max=None, cf='average', column_count=None):
        """
        Query interface method to retrieve the base rates (generally average 
        but could be delta as well).
        """
        cols = column_count
        if cols is None:
            ret_count = self.rates._column_family.multiget_count(
                self._get_row_keys(path,freq,ts_min,ts_max), 
                column_start=ts_min, column_finish=ts_max)
            cols = 0
            for i in ret_count.keys():
                cols += ret_count[i]
            cols += 5

        ret = self.rates._column_family.multiget(
                self._get_row_keys(path,freq,ts_min,ts_max), 
                column_start=ts_min, column_finish=ts_max,
                column_count=cols)
        
        if cf not in ['average', 'delta']:
            self.log.error('Not a valid option: %s - defaulting to average' % cf)
            cf = 'average'
        
        # Divisors to return either the average or a delta.
        if freq is None: freq = 1000
        value_divisors = { 'average': int(freq/1000), 'delta': 1 }
        
        # Just return the results and format elsewhere.
        results = []
        
        for k,v in ret.items():
            for kk,vv in v.items():
                results.append({'ts': kk, 'val': float(vv['val']) / value_divisors[cf], 
                                        'is_valid': vv['is_valid']})
            
        return results

    def query_aggregation_timerange(self, path=None, freq=None, 
                ts_min=None, ts_max=None, cf=None, column_count=None):
        """
        Query interface method to retrieve the aggregation rollups - could
        be average/min/max.  Different column families will be queried 
        depending on what value "cf" is set to.
        """
                
        if cf not in AGG_TYPES:
            self.log.error('Not a valid option: %s - defaulting to average' % cf)
            cf = 'average'
        
        if cf == 'average' or cf == 'raw':
            cols = column_count
            if cols is None:
                ret_count = self.aggs._column_family.multiget_count(
                        self._get_row_keys(path,freq,ts_min,ts_max),
                        column_start=ts_min, column_finish=ts_max)
                cols = 0
                for i in ret_count.keys():
                    cols += ret_count[i]
                cols += 5

            # print cols
            ret = self.aggs._column_family.multiget(
                    self._get_row_keys(path,freq,ts_min,ts_max), 
                    column_start=ts_min, column_finish=ts_max,
                    column_count=cols)

            # Just return the results and format elsewhere.
            results = []
            
            for k,v in ret.items():
                for kk,vv in v.items():
                    ts = kk
                    val = None
                    base_freq = None
                    count = None
                    for kkk in vv.keys():
                        if kkk == 'val':
                            val = vv[kkk]
                        else:
                            base_freq = kkk
                            count = vv[kkk]
                    ab = AggregationBin(**{'ts': ts, 'val': val,'base_freq': int(base_freq), 'count': count, 'cf': cf})
                    if cf == 'average':
                        datum = {'ts': ts, 'val': ab.average, 'cf': ab.cf}
                    else:
                        datum = {'ts': ts, 'val': ab.val, 'cf': ab.cf}
                    results.append(datum)
        elif cf == 'min' or cf == 'max':
            cols = column_count
            if cols is None:
                ret_count = self.stat_agg._column_family.multiget_count(
                        self._get_row_keys(path,freq,ts_min,ts_max),
                        column_start=ts_min, column_finish=ts_max)
                cols = 0
                for i in ret_count.keys():
                    cols += ret_count[i]
                cols += 5

            ret = self.stat_agg._column_family.multiget(
                    self._get_row_keys(path,freq,ts_min,ts_max), 
                    column_start=ts_min, column_finish=ts_max,
                    column_count=cols)
            
            results = []

            for k,v in ret.items():
                for kk,vv in v.items():
                    ts = kk
                    if cf == 'min':
                        datum = {'ts': ts, 'val': vv['min'], 'cf': cf, 'm_ts': vv.get('min_ts', None)}
                        results.append(datum)
                    else:
                        datum = {'ts': ts, 'val': vv['max'], 'cf': cf, 'm_ts': vv.get('max_ts', None)}
                        results.append(datum)
        
        return results
            
    def query_raw_data(self, path=None, freq=None,
                ts_min=None, ts_max=None, column_count=None):
        """
        Query interface to query the raw data.
        """
        cols = column_count
        if cols is None:
            ret_count = self.raw_data._column_family.multiget_count(
                    self._get_row_keys(path,freq,ts_min,ts_max),
                    column_start=ts_min, column_finish=ts_max)
            cols = 0
            for i in ret_count.keys():
                cols += ret_count[i]
            cols += 5

        ret = self.raw_data._column_family.multiget(
                self._get_row_keys(path,freq,ts_min,ts_max), 
                column_start=ts_min, column_finish=ts_max,
                column_count=cols)

        # Just return the results and format elsewhere.
        results = []

        for k,v in ret.items():
            for kk,vv in v.items():
                results.append({'ts': kk, 'val': json.loads(vv)})
        
        return results

    def query_raw_first(self, path=None, freq=None, year=None):
        """
        Query interface to query the raw data.
        """
        key = get_rowkey(path,freq,year)
        ret = self.raw_data._column_family.get(
                key,
                column_start="",
                column_count=1
                )
        # Just return the results and format elsewhere.
        results=[]
        for k,v in ret.items():
            results.append({'ts': k, 'val': json.loads(v)})
        return results

    def query_raw_last(self, path=None, freq=None, year=None):
        """
        Query interface to query the raw data.
        """
        key = get_rowkey(path,freq,year)
        ret = self.raw_data._column_family.get(
                key,
                column_finish="",
                column_reversed=True,
                column_count=1
                )
        # Just return the results and format elsewhere.
        results=[]
        for k,v in ret.items():
            results.append({'ts': k, 'val': json.loads(v)})
        return results

    def __del__(self):
        pass

# Stats/timing code for connection class

class DatabaseMetrics(object):
    """
    Code to handle calculating timing statistics for discrete database
    calls in the CASSANDRA_DB module.  Generally only used in development 
    to produce statistics when pushing runs of test data through it.
    """
    
    # List of attributes to generate/method names.
    _individual_metrics = [
        'raw_insert', 
        'baserate_update',
        'aggregation_update',
        'meta_fetch',
        'stat_fetch', 
        'stat_update',
    ]
    _all_metrics = _individual_metrics + ['total', 'all']
    
    def __init__(self, profiling=False):
        
        self.profiling = profiling
        
        if not self.profiling:
            return
        
        # Populate attrs from list.
        for im in self._individual_metrics:
            setattr(self, '%s_time' % im, 0)
            setattr(self, '%s_count' % im, 0)
        
    def _increment(self, m, t):
        """
        Actual logic called by named wrapper methods.  Increments
        the time sums and counts for the various db calls.
        """
        setattr(self, '%s_time' % m, getattr(self, '%s_time' % m) + t)
        setattr(self, '%s_count' % m, getattr(self, '%s_count' % m) + 1)
        
    # These are all wrapper methods that call _increment()

    def raw_insert(self, t):
        self._increment('raw_insert', t)

    def baserate_update(self, t):
        self._increment('baserate_update', t)

    def aggregation_update(self, t):
        self._increment('aggregation_update', t)
        
    def meta_fetch(self, t):
        self._increment('meta_fetch', t)
        
    def stat_fetch(self, t):
        self._increment('stat_fetch', t)

    def stat_update(self, t):
        self._increment('stat_update', t)
        
    def report(self, metric='all'):
        """
        Called at the end of a test harness or other loading dev script.  
        Outputs the various data to the console.
        """
        
        if not self.profiling:
            print 'Not profiling'
            return
        
        if metric not in self._all_metrics:
            print 'bad metric'
            return
            
        s = ''
        time = count = 0
            
        if metric in self._individual_metrics:
            datatype, action = metric.split('_')
            action = action.title()
            time = getattr(self, '%s_time' % metric)
            count = getattr(self, '%s_count' % metric)
            if time: # stop /0 errors
                s = '%s %s %s data in %.3f (%.3f per sec)' \
                    % (action, count, datatype, time, (count/time))
                if metric.find('total') > -1:
                    s += ' (informational - not in total)'
        elif metric == 'total':
            for k,v in self.__dict__.items():
                if k.find('total') > -1:
                    # don't double count the agg total numbers
                    continue
                if k.endswith('_count'):
                    count += v
                elif k.endswith('_time'):
                    time += v
                else:
                    pass
            if time:
                s = 'Total: %s db transactions in %.3f (%.3f per sec)' \
                    % (count, time, (count/time))
        elif metric == 'all':
            for m in self._all_metrics:
                if m == 'all':
                    continue
                else:
                    self.report(m)
                    
        if len(s): print s


# Data encapsulation objects - these objects wrap the various data
# in an object and provide utility methods and properties to convert 
# timestampes, calculate averages, etc.
        
class DataContainerBase(object):
    """
    Base class for the other encapsulation objects.  Mostly provides 
    utility methods for subclasses.
    """
    
    _doc_properties = []
    
    def __init__(self, path):
        self.path = path
        
    def _handle_date(self,d):
        """
        Return a datetime object given a JavaScript timestamp.
        """

        if type(d) == datetime.datetime:
            return d
        else:
            return datetime.datetime.utcfromtimestamp(float(d)/1000.0)

    def get_document(self):
        """
        Return a dictionary of the attrs/props in the object.
        """
        doc = {}
        for k,v in self.__dict__.items():
            if k.startswith('_'):
                continue
            doc[k] = v
            
        for p in self._doc_properties:
            doc[p] = getattr(self, '%s' % p)
        
        return doc

    def get_key(self):
        """
        Return a cassandra row key based on the contents of the object.
        """
        return get_rowkey(self.path)
        
    def ts_to_jstime(self, t='ts'):
        """
        Return an internally represented datetime value as a JavaScript
        timestamp which is milliseconds since the epoch (Unix timestamp * 1000).
        Defaults to returning 'ts' property, but can be given an arg to grab a
        different property/attribute like Metadata.last_update.
        """
        ts = getattr(self, t)
        return calendar.timegm(ts.utctimetuple()) * 1000

    def ts_to_unixtime(self, t='ts'):
        """
        Return an internally represented datetime value as a Unix timestamp.
        Defaults to returning 'ts' property, but can be given an arg to grab a
        different property/attribute like Metadata.last_update.
        """
        ts = getattr(self, t)
        return calendar.timegm(ts.utctimetuple())

class RawData(DataContainerBase):
    """
    Container for raw data rows.

    Can be instantiated from args when reading from persist queue, or via **kw
    when reading data back out of Cassandra.
    """
    _doc_properties = ['ts']

    def __init__(self, path=None, ts=None, val=None):
        DataContainerBase.__init__(self, path)
        self._ts = None
        self.ts = ts
        self.val = val

    def get_key(self):
        """
        Return a cassandra row key based on the contents of the object.

        We append the year to the row key to limit the size of each row to only
        one year's worth of data.  This is an implementation detail for using
        Cassandra effectively.
        """
        return get_rowkey(self.path, year=self.ts.year)

    @property
    def ts(self):
        return self._ts
        
    @ts.setter
    def ts(self, value):
        self._ts = self._handle_date(value)


class RawRateData(RawData):
    """
    Container for raw data for rate based rows.
    """
    _doc_properties = ['ts']

    def __init__(self, path=None, ts=None, val=None, freq=None):
        RawData.__init__(self, path, ts, val)
        self.freq = freq

    def __unicode__(self):
        return "<RawRateData/%d: ts=%s, val=%s, path=%s>" % \
            (id(self), self.ts, self.val, self.path)

    def __repr__(self):
        return "<RawRateData/%d: ts=%s, val=%s, path=%s>" % \
            (id(self), self.ts, self.val, self.path)

    def get_key(self):
        """
        Return a cassandra row key based on the contents of the object.

        For rate data we add the frequency to the row key before the year, see
        the RawData.get_key() documentation for details about the year.
        """
        return get_rowkey(self.path, freq=self.freq, year=self.ts.year)

    def get_meta_key(self):
        """
        Get a "metadata row key" - metadata don't have timestamps/years.
        Other objects use this to look up entires in the metadata_cache.
        """
        return get_rowkey(self.path, freq=self.freq)
        
    @property
    def min_last_update(self):
        return self.ts_to_jstime() - self.freq * 40
        
    @property
    def slot(self):
        return (self.ts_to_jstime() / self.freq) * self.freq
    
        
class Metadata(DataContainerBase):
    """
    Container for metadata information.
    """
    
    _doc_properties = ['min_ts', 'last_update']
    
    def __init__(self, path=None, last_update=None, last_val=None, min_ts=None, freq=None):
        DataContainerBase.__init__(self, path)
        self._min_ts = self._last_update = None
        self.last_update = last_update
        self.last_val = last_val
        self.min_ts = min_ts
        self.freq = freq

    def __unicode__(self):
        return "<Metadata/%d: last_update=%s, last_val=%s, min_ts=%s, freq=%s>" % \
            (id(self), self.last_update, self.last_val, self.min_ts, self.freq)

    def __repr__(self):
        return "<Metadata/%d: last_update=%s, last_val=%s, min_ts=%s, freq=%s>" % \
            (id(self), self.last_update, self.last_val, self.min_ts, self.freq)
        
    @property
    def min_ts(self):
        return self._min_ts
        
    @min_ts.setter
    def min_ts(self, value):
        self._min_ts = self._handle_date(value)
    
    @property
    def last_update(self):
        return self._last_update
        
    @last_update.setter
    def last_update(self, value):
        self._last_update = self._handle_date(value)
        
    def refresh_from_raw(self, data):
        """
        Update the internal state of a metadata object from a raw data
        object.  This is called by the persister when calculating 
        base rate deltas to refresh cache with current values after a 
        successful delta is generated.
        """
        if self.min_ts > data.ts:
            self.min_ts = data.ts
        self.last_update = data.ts
        self.last_val = data.val
        

class BaseRateBin(RawRateData):
    """
    Container for base rates.  Has 'average' property to return the averages.
    """
    
    _doc_properties = ['ts']
    
    def __init__(self, path=None, ts=None, val=None, freq=None, is_valid=1):
        RawRateData.__init__(self, path, ts, val, freq)
        self.is_valid = is_valid

    @property
    def average(self):
        return self.val / self.freq
    

class AggregationBin(BaseRateBin):
    """
    Container for aggregation rollups.  Also has 'average' property to generage averages.
    """
    
    def __init__(self, path=None, ts=None, val=None, freq=None, base_freq=None, count=None, 
            min=None, max=None, cf=None):
        BaseRateBin.__init__(self, path, ts, val, freq)
        
        self.count = count
        self.min = min
        self.max = max
        self.base_freq = base_freq
        self.cf = cf
        
    @property
    def average(self):
        return self.val / (self.count * (self.base_freq/1000.0))

def escape_path(path):
    escaped = []
    for step in path:
        escaped.append(step.replace(KEY_DELIMITER, 
            "\\%s" % KEY_DELIMITER))

    return escaped

def get_rowkey(path, freq=None, year=None):
    """
    Given a path and some additional data build the Cassandra row key.

    The freq and year arguments are used for internal book keeping inside
    Cassandra.
    """


    appends = []
    if freq:
        appends.append(str(freq))
    if year:
        appends.append(str(year))

    return KEY_DELIMITER.join(escape_path(path) + appends)

def _split_rowkey(s, escape='\\'):
    """
    Return the elements of the rowkey taking escaping into account.

    FOR INTERNAL USE ONLY!  This returns more than just the path in most
    instances and needs to be used with specific knowledge of what kind of row
    key is used.
    """
    indices = []

    for i in range(len(s)):
        if s[i] == KEY_DELIMITER:
            if i > 0 and s[i-1] != escape:
                indices.append(i)
            elif i == 0:
                indices.append(i)

    out = []
    last = 0
    for i in indices:
        out.append(s[last:i].replace(escape, ""))
        last = i+1
    out.append(s[last:])

    return out
