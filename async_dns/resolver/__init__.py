'''
Asynchronous DNS client
'''
import asyncio
import os
from .. import *
from . import tcp, udp
from ..cache import DNSMemCache

A_TYPES = types.A, types.AAAA

class Resolver:
    '''
    Asynchronous DNS resolver.
    '''
    recursive = 1

    def __init__(self, protocol=UDP, request_timeout=3.0, timeout=3.0):
        self.futures = {}
        cache = DNSMemCache()
        self.cache = cache
        self.protocol = InternetProtocol.get(protocol)
        self.request_timeout = request_timeout
        self.timeout = timeout

    async def query_cache(self, res, fqdn, qtype):
        '''Returns a boolean whether a cache hit occurs.'''
        # if cached CNAME
        cname = list(self.cache.query(fqdn, types.CNAME))
        if cname:
            res.an.extend(cname)
            if not self.recursive or qtype == types.CNAME:
                return True
            for rec in cname:
                cres = await self.query(rec.data, qtype)
                if cres is None or cres.r > 0:
                    continue
                res.an.extend(cres.an)
                res.ns = cres.ns
                res.ar = cres.ar
            return True
        # else
        data = list(self.cache.query(fqdn, qtype))
        cache_hit = False
        if data:
            for rec in data:
                if rec.qtype in (types.NS,):
                    nres = list(self.cache.query(rec.data, A_TYPES))
                    if nres:
                        res.ar.extend(nres)
                        res.ns.append(rec)
                        if rec.qtype == qtype:
                            cache_hit = True
                else:
                    res.an.append(rec.copy(name=fqdn))
                    if qtype == types.CNAME or rec.qtype != types.CNAME:
                        cache_hit = True
        return cache_hit

    def get_nameservers(self, fdqn):
        filename='/etc/resolv.conf'
        nameservers = []
        with open(filename, 'r') as file:
            for line in file:
                if line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                if parts[0] == 'nameserver':
                    nameservers.append(parts[1])
        return address.NameServers(nameservers)

    async def request(self, req, addr, protocol=None):
        '''Return response to a request.

        Send DNS request data according to `protocol`.
        '''
        if protocol is None:
            protocol = self.protocol
        if protocol is TCP:
            request = tcp.request
        else:
            request = udp.request
        data = await request(req, addr, self.request_timeout)
        return data

    async def get_remote(self, nameservers, req, future=None):
        while True:
            if future and future.cancelled():
                break
            addr = nameservers.get()
            try:
                data = await self.request(req, addr)
                cres = DNSMessage.parse(data)
                assert cres.r != 2
            except (asyncio.TimeoutError, AssertionError):
                nameservers.fail(addr)
            except DNSError:
                pass
            else:
                return cres

    async def query_remote(self, res, fqdn, qtype):
        '''Return a boolean indicating whether results are found.

        No cache is used and requests are sent to remote servers.
        '''
        if fqdn.endswith('.in-addr.arpa'):
            # Reverse DNS lookup only occurs locally
            return
        # look up from other DNS servers
        nameservers = self.get_nameservers(fqdn)
        cname = [fqdn]
        req = DNSMessage(qr=REQUEST)
        has_result = False
        key = fqdn, qtype
        future = self.futures.get(key)
        while not has_result:
            if not cname:
                break
            # seems that only one qd is supported by most NS
            req.qd = [Record(REQUEST, cname[0], qtype)]
            del cname[:]
            cres = await self.get_remote(nameservers, req, future)
            if not cres: break
            for rec in cres.an + cres.ns + cres.ar:
                if rec.ttl > 0 and rec.qtype not in (types.SOA, types.MX):
                    self.cache.add_host(rec)
            for rec in cres.an:
                res.an.append(rec)
                if rec.qtype == types.CNAME:
                    cname.append(rec.data)
                if qtype == types.CNAME or rec.qtype != types.CNAME:
                    has_result = True
            for rec in cres.ns:
                if not self.recursive:
                    res.ns.append(rec)
                    has_result = True
                elif rec.qtype == types.SOA or qtype == types.NS:
                    has_result = True
            if not self.recursive:
                res.ar.extend(cres.ar)
            nameservers = address.NameServers(i.data for i in cres.ar if i.qtype in A_TYPES)
            if not nameservers:
                for ns_r in cres.ns:
                    host = ns_r.data.mname if ns_r.qtype == types.SOA else ns_r.data
                    try:
                        ns_res = await self.query(host)
                        assert ns_res
                    except (AssertionError, asyncio.TimeoutError):
                        pass
                    except Exception as e:
                        logger.error(host)
                        logger.error(e)
                    else:
                        if ns_res:
                            for ans in ns_res.an:
                                if ans.qtype in A_TYPES:
                                    nameservers.add(ans.data)
            res.r = cres.r
        return has_result

    async def query(self, fqdn, qtype=types.ANY, timeout=None):
        '''Return query result.

        Cache queries for hostnames and types to avoid repeated requests at the same time.
        '''
        key = fqdn, qtype
        future = self.futures.get(key)
        if future is None:
            loop = asyncio.get_event_loop()
            future = self.futures[key] = loop.create_future()
            asyncio.ensure_future(self.do_query(fqdn, qtype))
        if timeout is None:
            timeout = self.timeout
        try:
            res = await asyncio.wait_for(future, timeout)
        except (AssertionError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        else:
            return res

    async def do_query(self, fqdn, qtype):
        '''
        Starts a query asynchronously, add the future object to cache.
        '''
        key = fqdn, qtype
        res = DNSMessage(ra=self.recursive)
        res.qd.append(Record(REQUEST, name=fqdn, qtype=qtype))
        future = self.futures[key]
        ret = (
            await self.query_cache(res, fqdn, qtype)
        ) or (
            await self.query_remote(res, fqdn, qtype)
        )
        if not ret and not res.r:
            res.r = 2
        self.futures.pop(key)
        if not future.cancelled():
            future.set_result(res)
