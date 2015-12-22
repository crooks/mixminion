# Copyright 2002-2011 Nick Mathewson.  See LICENSE for licensing information.

"""mixminion.testSupport

   Shared support code for unit tests, benchmark tests, and integration tests.
   """

import base64
import cStringIO
import os
import stat
import sys

import mixminion.Crypto
import mixminion.Common
from mixminion.Common import waitForChildren, ceilDiv, createPrivateDir, LOG
from mixminion.Config import _parseBoolean, _parseIntervalList, ConfigError

from mixminion.server.Modules import DELIVER_FAIL_NORETRY, DELIVER_FAIL_RETRY,\
     DELIVER_OK, DeliveryModule, ImmediateDeliveryQueue, \
     SimpleModuleDeliveryQueue, _escapeMessageForEmail

#----------------------------------------------------------------------
# DirectoryStoreModule

class DirectoryStoreModule(DeliveryModule):
    """Delivery module for testing: puts messages in files in a given
       directory.  Can be configured to use a delivery queue or not.

       When this module delivers a message:
       If the routing info is 'FAIL!', the message is treated as undeliverable.
       If the routing info is 'fail', the message is treated as temporarily
         undeliverable (and so will eventually time out).
       Otherwise, creates a file in the specified directory, containing
          the routing info, a newline, and the message contents.
    """
    def __init__(self):
        DeliveryModule.__init__(self)

    ## Fields:
    # loc -- The directory to store files in.  All filenames are numbers;
    #    we always put new messages in the smallest number greater than
    #    all existing numbers.
    # next -- the number of the next file.
    def getConfigSyntax(self):
        return { 'Testing/DirectoryDump':
                 { 'Location' : ('REQUIRE', None, None),
                   'UseQueue': ('REQUIRE', "boolean", None),
                   'Retry' : ('ALLOW', "intervalList",
                              "every 1 min for 10 min") } }

    def validateConfig(self, config, lines, contents):
        # loc = sections['Testing/DirectoryDump'].get('Location')
        pass

    def getRetrySchedule(self):
        return self.retry

    def configure(self, config, manager):
        self.loc = config['Testing/DirectoryDump'].get('Location')
        if not self.loc:
            return
        self.useQueue = config['Testing/DirectoryDump']['UseQueue']

        if not os.path.exists(self.loc):
            createPrivateDir(self.loc)

        self.next = 1 + max([-1]+[int(f) for f in os.listdir(self.loc)])

        self.retry = config['Testing/DirectoryDump']['Retry']
        manager.enableModule(self)

    def getServerInfoBlock(self):
        return ""

    def getName(self):
        return "Testing_DirectoryDump"

    def getExitTypes(self):
        return [ 0xFFFE ]

    def createDeliveryQueue(self, queueDir):
        if self.useQueue:
            return SimpleModuleDeliveryQueue(self, queueDir,
                                             retrySchedule=self.retry)
        else:
            return ImmediateDeliveryQueue(self)

    def processMessage(self, packet):
        assert packet.getExitType() == 0xFFFE
        exitInfo = packet.getAddress()

        if exitInfo == 'fail':
            return DELIVER_FAIL_RETRY
        elif exitInfo == 'FAIL!':
            return DELIVER_FAIL_NORETRY

        log.debug("Delivering test message")

        m = _escapeMessageForEmail(packet)
        if m is None:
            # Ordinarily, we'd drop corrupt messages, but this module is
            # meant for debugging.
            m = """\
==========CORRUPT OR UNDECODABLE MESSAGE
Decoding handle: %s%s==========MESSAGE ENDS""" % (
                      base64.encodestring(packet.getTag()),
                      base64.encodestring(packet.getContents()))

        f = open(os.path.join(self.loc, str(self.next)), 'w')
        self.next += 1
        f.write(m)
        f.close()
        return DELIVER_OK

#----------------------------------------------------------------------
# mix_mktemp: A secure, paranoid mktemp replacement.  (May be overkill
# for testing, but better safe than sorry.)

# Name of our temporary directory: all temporary files go under this
# directory.  If None, it hasn't been created yet.  If it exists,
# it must be owned by us, mode 700.

_CHECK_MODE = 1
_CHECK_UID = 1
if sys.platform in ('cygwin', 'win32') or os.environ.get("MM_NO_FILE_PARANOIA"):
    _CHECK_MODE = _CHECK_UID = 0

_MM_TESTING_TEMPDIR = None
# How many temporary files have we created so far?
_MM_TESTING_TEMPDIR_COUNTER = 0
# Do we nuke the contents of _MM_TESTING_TEMPDIR on exit?
_MM_TESTING_TEMPDIR_REMOVE_ON_EXIT = 1
def mix_mktemp(extra=""):
    '''mktemp wrapper. puts all files under a securely mktemped
       directory.'''
    global _MM_TESTING_TEMPDIR
    global _MM_TESTING_TEMPDIR_COUNTER
    if _MM_TESTING_TEMPDIR is None:
        # We haven't configured our temporary directory yet.
        import tempfile

        # If tempfile.mkdtemp exists, use it.  This avoids warnings, and
        # is harder for people to exploit.
        if hasattr(tempfile, 'mkdtemp'):
            try:
                temp = tempfile.mkdtemp()
            except OSError, e:
                print "mkdtemp failure: %s" % e
                sys.exit(1)
        else:
        # Otherwise, pick a dirname, make sure it doesn't exist, and try to
        # create it.
            temp = tempfile.mktemp()
            if os.path.exists(temp):
                print "I think somebody's trying to exploit mktemp."
                sys.exit(1)
            try:
                os.mkdir(temp, 0700)
            except OSError, e:
                print "Something's up with mktemp: %s" % e
                sys.exit(1)

        # The directory must exist....
        if not os.path.exists(temp):
            print "Couldn't create temp dir %r" %temp
            sys.exit(1)
        st = os.stat(temp)

        # And be writeable only by us...
        if _CHECK_MODE and st[stat.ST_MODE] & 077:
            print "Couldn't make temp dir %r with secure permissions" %temp
            sys.exit(1)
        # And be owned by us...
        if _CHECK_UID and st[stat.ST_UID] != os.getuid():
            print "The wrong user owns temp dir %r"%temp
            sys.exit(1)

        _MM_TESTING_TEMPDIR = temp
        if _MM_TESTING_TEMPDIR_REMOVE_ON_EXIT:
            import atexit
            atexit.register(deltree, temp)

    # So now we have a temporary directory; return the name of a new
    # file there.
    _MM_TESTING_TEMPDIR_COUNTER += 1
    return os.path.join(_MM_TESTING_TEMPDIR,
                        "tmp%05d%s" % (_MM_TESTING_TEMPDIR_COUNTER,extra))

_WAIT_FOR_KIDS = 1
def deltree(*dirs):
    """Delete each one of a list of directories, along with all of its
       contents."""
    global _WAIT_FOR_KIDS
    #print "deltree(%r)"%dirs
    if _WAIT_FOR_KIDS:
        print "Waiting for shred processes to finish."
        waitForChildren()
        _WAIT_FOR_KIDS = 0
    for d in dirs:
        #print "Considering",d
        if os.path.isdir(d):
            #print "deleting from %s: %s" % (d, os.listdir(d))
            for fn in os.listdir(d):
                loc = os.path.join(d,fn)
                if os.path.isdir(loc):
                    #print "deleting (I)",loc
                    deltree(loc)
                else:
                    #print "unlinking (I)",loc
                    os.unlink(loc)
            #ld = os.listdir(d)
            #if ld: print "remaining in %s: %s" % (d, ld)
            if os.listdir(d):
                print "os.listdir(%r)==(%r)"%(d,os.listdir(d))
            os.rmdir(d)
        elif os.path.exists(d):
            #print "Unlinking", d
            os.unlink(d)
        else:
            pass #print "DNE", d

#----------------------------------------------------------------------
# suspendLog

def suspendLog(severity=None):
    """Temporarily suppress logging output"""
    log = LOG
    if hasattr(log, '_storedHandlers'):
        resumeLog()
    buf = cStringIO.StringIO()
    h = mixminion.Common._ConsoleLogHandler(buf)
    log._storedHandlers = log.handlers
    log._storedSeverity = log.severity
    log._testBuf = buf
    log.handlers = []
    if severity is not None:
        log.setMinSeverity(severity)
    log.addHandler(h)

def resumeLog():
    """Resume logging output.  Return all new log messages since the last
       suspend."""
    log = LOG
    if not hasattr(log, '_storedHandlers'):
        return None
    buf = log._testBuf
    del log._testBuf
    log.handlers = log._storedHandlers
    del log._storedHandlers
    log.setMinSeverity(log._storedSeverity)
    del log._storedSeverity
    return buf.getvalue()

#----------------------------------------------------------------------
# Facilities to temporarily replace attributes and functions for testing

# List of object, attribute, old-value for all replaced attributes.
_REPLACED_OBJECT_STACK = []

def replaceAttribute(object, attribute, value):
    """Temporarily replace <object.attribute> with value.  When
       undoReplacedAttributes() is called, the old value is restored."""
    if hasattr(object, attribute):
        tup = (object, attribute, getattr(object, attribute))
    else:
        tup = (object, attribute)
    _REPLACED_OBJECT_STACK.append(tup)
    setattr(object, attribute, value)

# List of (fnname, args, kwargs) for all the replaced functions that
# have been called.
_CALL_LOG = []

class _ReplacementFunc:
    """Helper object: callable stub that logs its invocations to _CALL_LOG
       and delegates to an internal function."""
    def __init__(self, name, fn=None):
        self.name = name
        self.fn = fn
    def __call__(self, *args, **kwargs):
        _CALL_LOG.append((self.name, args, kwargs))
        if self.fn:
            return self.fn(*args, **kwargs)
        else:
            return None

def replaceFunction(object, attribute, fn=None):
    """Temporarily replace the function or method <object.attribute>.
       If <fn> is provided, replace it with fn; otherwise, the new
       function will just return None.  All invocations of the new
       function will logged, and retrievable by getReplacedFunctionCallLog()"""
    replaceAttribute(object, attribute, _ReplacementFunc(attribute, fn))

def getReplacedFunctionCallLog():
    """Return a list of (functionname, args, kwargs)"""
    return _CALL_LOG

def clearReplacedFunctionCallLog():
    """Clear all entries from the replaced function call log"""
    del _CALL_LOG[:]

def undoReplacedAttributes():
    """Undo all replaceAttribute and replaceFunction calls in effect."""
    # Remember to traverse _REPLACED_OBJECT_STACK in reverse, so that
    # "replace(a,b,c1); replace(a,b,c2)" is properly undone.
    r = _REPLACED_OBJECT_STACK[:]
    r.reverse()
    del _REPLACED_OBJECT_STACK[:]
    for item in r:
        if len(item) == 2:
            o,a = item
            delattr(o,a)
        else:
            o,a,v = item
            setattr(o,a,v)

#----------------------------------------------------------------------
# Test vectors.

class CyclicRNG(mixminion.Crypto.RNG):
    def __init__(self):
        mixminion.Crypto.RNG.__init__(self,4096)
        self.idx = 0
        self.pattern = "".join(map(chr,range(256)))
    def _prng(self,n):
        reps = ceilDiv(n+self.idx,256)
        r = (self.pattern*reps)[self.idx:self.idx+n]
        self.idx = (self.idx+n) % 256
        assert len(r) == n
        return r

def unHexStr(s):
    assert s[0] == '['
    assert s[-1] == ']'
    r = []
    for i in xrange(1,len(s)-1,3):
        r.append(chr(int(s[i:i+2],16)))
        assert s[i+2] in ' ]'
    return "".join(r)

def unHexNum(s):
    assert s[0] == '['
    assert s[-1] == ']'
    r = [  ]
    for i in xrange(1,len(s)-1,3):
        r.append(s[i:i+2])
        assert s[i+2] in ' ]'
    return long("".join(r), 16)

def hexStr(s):
    r = []
    for c in s:
        r.append("%02X"%ord(c))
    return "[%s]"%(" ".join(r))

def hexNum(n):
    hn = "%X"%n
    if len(hn)%2 == 1:
        hn = "0"+hn
    r = []
    for i in xrange(0,len(hn),2):
        r.append(hn[i:i+2])
    return "[%s]"%(" ".join(r))

def tvRSA():
    print "======================================== RSA"
    pk1 = TEST_KEYS_2048[0]
    print "Example 2048-bit Key K"
    n,e = pk1.get_public_key()
    n2,e2,d,p,q = pk1.get_private_key()
    print "   exponent =",hexNum(e)
    print "   modulus =",hexNum(n)
    print "   Private key (P)=",hexNum(p)
    print "   Private key (Q)=",hexNum(q)
    print "   Private key (D)=",hexNum(d)

    print "   PK_Encode(K) =",hexStr(pk1.encode_key(1))
    print "   Fingerprint =",mixminion.Crypto.pk_fingerprint(pk1)

    print
    ms = CyclicRNG().getBytes(20)
    print "OAEP Padding/PKCS encoding example: (Using MGF SEED %s)"%hexStr(ms)
    s = "Hello world"
    print "   original string M:",hexStr(s)
    assert pk1.get_modulus_bytes() == 256
    enc = mixminion.Crypto._add_oaep_padding(s,
                mixminion.Crypto.OAEP_PARAMETER,256,CyclicRNG())
    print "   Padded string (2048 bits):",hexStr(enc)
    pkenc = pk1.crypt(enc,1,1)

    print
    print "   PK_Encrypt(K,M):",hexStr(pkenc)
    assert mixminion.Crypto.pk_decrypt(pkenc,pk1) == s

def tvAES():
    import mixminion._minionlib as _ml
    print "======================================== AES"
    print "Single block encryption"
    k = unHexStr("[00 11 22 33 44 55 66 77 88 99 AA BB CC DD EE FF]")
    b = "MixminionTypeIII"
    print "   Key:",hexStr(k)
    print "   Plaintext block:",hexStr(b)
    eb = _ml.aes128_block_crypt(_ml.aes_key(k),b,1)
    db = _ml.aes128_block_crypt(_ml.aes_key(k),b,0)
    print "   Encrypted block:",hexStr(eb)
    print "   Decrypted block:",hexStr(db)

    print
    print "Counter mode encryption:"
    k = unHexStr("[02 13 24 35 46 57 68 79 8A 9B AC BD CE DF E0 F1]")
    print "   Key:",hexStr(k)
    print "   Keystream[0x00000...0x0003F]:",hexStr(mixminion.Crypto.prng(k,64))
    print "   Keystream[0x002C0...0x002FF]:",hexStr(mixminion.Crypto.prng(k,64,0x2c0))
    print "   Keystream[0xF0000...0xF003F]:",hexStr(mixminion.Crypto.prng(k,64,0xF0000))

    txt = "Hello world!"
    print "   Example text M:",hexStr(txt)
    print "   Encrypt(K,M):",hexStr(mixminion.Crypto.ctr_crypt(txt,k))

def tvLIONESS():
    print "======================================== LIONESS"
    print "SPRP_Encrypt:"
    ks = mixminion.Crypto.Keyset("basic key")
    k1,k2,k3,k4=ks.getLionessKeys("A")
    print " Base key K:",hexStr(k1)
    print "         K2:",hexStr(k2)
    print "         K3:",hexStr(k3)
    print "         K4:",hexStr(k4)
    txt = "I never believe in code until it's running, and I never believe in the next release until it's out."
    print
    print "      Example text M:",hexStr(txt)
    print "   SPRP_Encrypt(K,M):",hexStr(mixminion.Crypto.lioness_encrypt(
        txt,(k1,k2,k3,k4)))
    print "   SPRP_Decrypt(K,M):",hexStr(mixminion.Crypto.lioness_decrypt(
        txt,(k1,k2,k3,k4)))

def testVectors(name,args):
    assert hexStr("ABCDEFGHI") == "[41 42 43 44 45 46 47 48 49]"
    assert hexNum(10000) == '[27 10]'
    assert hexNum(100000) == '[01 86 A0]'
    assert hexNum(1000000000L) == '[3B 9A CA 00]'

    assert unHexStr(hexStr("ABCDEFGHI")) == "ABCDEFGHI"
    assert unHexNum(hexNum(10000))  in (10000, 10000L)
    assert unHexNum(hexNum(100000)) in (100000,100000L)
    assert unHexNum(hexNum(1000000000L)) == 1000000000L

    tvRSA()
    tvAES()
    tvLIONESS()

#----------------------------------------------------------------------
# Long keypairs: stored here to avoid regenerating them every time we need
# to run tests.  (We can't use 1024-bit keys, since they're not long enough
# to use as identity keys.)

TEST_KEYS_2048 = [
"""\
MIIEowIBAAKCAQEA0aBBHqAyfoAweyq5NGozHezVut12lGHeKrfmnax9AVPMfueqskqcKsjMe3Rz
NhDukD3ebYKPLKMnVDM+noVyHSawnzIc+1+wq1LFP5TJiPkPdodKq/SNlz363kkluLwhoWdn/16k
jlprnvdDk6ZxuXXTsAGtg235pEtFs4BLOLOxikW2pdt2Tir71p9SY0zGdM8m5UWZw4z3KqYFfPLI
oBsN+3hpcsjjO4BpkzpP3zVxy8VN2+hCxjbfow2sO6faD2u6r8BXPB7WlAbmwD8ZoX6f8Fbay02a
jG0mxglE9f0YQr66DONEQPoxQt8C1gn3KAIQ2Hdw1cxpQf3lkceBywIDAQABAoIBAETRUm+Gce07
ki7tIK4Ha06YsLXO/J3L306w3uHGfadQ5mKHFW/AtLILB65D1YrbViY+WWYkJXKnAUNQK2+JKaRO
Tk+E+STBDlPAMYclBmCUOzJTSf1XpKARNemBpAOYp4XAV9DrNiSRpKEkVagETXNwLhWrB1aNZRY9
q9048fjj1NoXsvLVY6HTaViHn8RCxuoSHT/1LXjStvR9tsLHk6llCtzcRO1fqBH7gRog8hhL1g5U
rfUJnXNSC3C2P9bQty0XACq0ma98AwGfozrK3Ca40GtlqYbsNsbKHgEgSVe124XDeVweK8b56J/O
EUsWF5hwdZnBTfmJP8IWmiXS16ECgYEA8YxFt0GrqlstLXEytApkkTZkGDf3D1Trzys2V2+uUExt
YcoFrZxIGLk8+7BPHGixJjLBvMqMLNaBMMXH/9HfSyHN3QHXWukPqNhmwmnHiT00i0QsNsdwsGJE
xXH0HsxgZCKDkLbYkzmzetfXPoaP43Q5feVSzhmBrZ3epwlTJDECgYEA3isKtLiISyGuao4bMT/s
3sQcgqcLArpNiPUo5ESp5qbXflAiH2wTC1ZNh7wUtn0Am8TdG1JnKFUdwHELpiRP9yCQj2bFS/85
jk6RCEmXdAGpYzB6lrqtYhFNe5LzphLGtALsuVOq6I7LQbUXY3019fkawfiFvnYZVovC3DKCsrsC
gYBSg8y9EZ4HECaaw3TCtFoukRoYe+XWQvhbSTPDIs+1dqZXJaBS8nRenckLYetklQ8PMX+lcrv4
BT8U3ju4VIWnMOEWgq6Cy+MhlutjtqcHZvUwLhW8kN0aJDfCC2+Npdu32WKAaTYK9Ucuy9Un8ufs
l6OcMl7bMTNvj+KjxTe1wQKBgB1cSNTrUi/Dqr4wO429qfsipbXqh3z7zAVeiOHp5R4zTGVIB8pp
SPcFl8dpZr9bM7piQOo8cJ+W6BCnn+d8Awlgx1n8NfS+LQgOgAI9X4OYOJ+AJ6NF1mYQbVH4cLSw
5Iujm08+rGaBgIEVgprGUFxKaGvcASjTiLO0UrMxBa7DAoGBALIwOkPLvZNkyVclSIdgrcWURlyC
oAK9MRgJPxS7s6KoJ3VXVKtIG3HCUXZXnmkPXWJshDBHmwsv8Zx50f+pqf7MD5fi3L1+rLjN/Rp/
3lGmzcVrG4LO4FEgs22LXKYfpvYRvcsXzbwHX33LnyLeXKrKYQ82tdxKOrh9wnEDqDmh""",
"""\
MIIEpQIBAAKCAQEAv/fvw/2HK48bwjgR2nUQ1qea9eIsYv4m98+DQoqPO7Zlr+Qs6/uiiOKtH0/b
3/B9As261HKkI4VDG0L523rB1QAfeENKdLczj8DoQPjHMsNDDepbBYmYH91vmig47fbLmbDnUiSD
+CFtM+/wUG4holomQBdPfUhoc44Fcw3cyvskkJr5aN9rqBRGuwuR81RaXt5lKtiwv9JUYqEBb2/f
sSDEWWHSf9HemzR25M/T+A51yQwKyFXC4RQzCu2jX7sZ53c6KRCniLPq9wUwtTrToul34Sssnw8h
PiV0Fwrk12uJdqqLDbltUlp6SEx8vBjSZC6JnVsunYmw88sIYGsrbQIDAQABAoIBAQCpnDaLxAUZ
x2ePQlsD2Ur3XT7c4Oi2zjc/3Gjs8d97srxFnCTUm5APwbeUYsqyIZlSUNMxwdikSanw/EwmT1/T
AjjL2Sh/1x4HdTm/rg7SGxOzx8yEJ/3wqYVhfwhNuDBLqrG3Mewn3+DMcsKxTZ0KBPymw/HHj6I5
9tF5xlW+QH7udAPxAX3qZC/VveqlomGTu4rBBtGt1mIIt+iP4kjlOjIutb6EK3fXZ8r9VZllNJ3D
/xZVx7Jt40hcV6CEuWOg1lwXQNmgl8+bSUvTaCpiVQ4ackeosWhTWxtKndw4UXSzXZAbjHAmAwMY
bHwxN4AqZZfbb2EI1WzOBjeZje1BAoGBAOiQZgngJr++hqn0gJOUImv+OWpFMfffzhWyMM8yoPXK
tIKaFTEuHAkCVre6lA1g34cFeYDcK9BC4oyQbdO4nxTZeTnrU2JQK2t4+N7WBU9W5/wOlxEdYzE0
2rNrDxBtOtCQnOI1h9Mrc87+xzPP55OloKbRMW1JzeAxWdg1LJrvAoGBANNQRNdRzgoDAm0N7WNe
pGx51v+UuGUHvE4dMGKWdK8njwbsv6l7HlTplGGOZUThZWM7Ihc8LU6NZ2IlwWYgzivYL/SUejUD
9/rYaWEYWPdXQW2/ekdi3FFZtKcuUB5zLy3gqtLSjM1a8zhbxdkYq4tqa+v9JwMTr/oyVf//XM9j
AoGAEjftpmxm3LKCPiInSGhcYfVibg7JoU9pB44UAMdIkLi2d1y2uEmSbKpAPNhi7MFgAWXOZOfa
jtAOi1BtKh7WZ325322t9I+vNxYc+OfvNo3qUnaaIv8YXCx1zYRfg7vq1ZfekmH7J/HJere+xzJM
Q+a/tRHCO3uCo0N6dFOGEQUCgYEAsQhJdD6zqA2XZbfKTnrGs55rsdltli6h4qtvktjLzsYMfFex
xpI/+hFqX0TFsKxInZa329Ftf6bVmxNYcHBBadgHbRdLPskhYsUVm+Oi/Szbws8s6Ut4mqrVv038
j1Yei4fydQcyMQTmSSwRl+ykIvu4iI+gtGI1Bx5OkFbm8VMCgYEAlEvig/fGBA/MgE6DUf6MXbFn
92JW25az5REkpZtEXz3B6yhyt/S5D1Da6xvfqvNijyqZpUqtp7lPSOlqFRJ3NihNc8lRqyFMPiBn
41QQWPZyFa1rTwJxijyG9PkI0sl1/WQK5QrTjGZGjX7r4Fjzr6EYM8gH3RA3WAPzJylTOdo=""",
"""\
MIIEpQIBAAKCAQEA68uqw2Ao12QPktY9pf9VSHMfJ8jKBGG4eG+HPmaBifc6+kAZWA7jeOwMTnbS
+KZ2nMFXKthp6zJiDzQqgKlQ7eA0zzBPtAboy4YhPRwrrQr/o1oPrppS2eEwvCGewySAZsIUwX4d
0P68lpLbA9h1vuV3t19M2WNifsYYcTUGPGdbpZHgBDQdmQeUBkXtCTANPxOYsrLwEhaCBrK4BLkW
sRNi0dRnFRdJ18rAYCiDAKq168IyP4TCUKKGWHbquv5rrNdg/RoUiCyPTgDodLaXTOLrRPuCOl5p
dwhNSwJyzEpeqy/x4YnNRbGNv7M1sNhnrarbUduZqOz9RpTQ0niKFQIDAQABAoIBAQC2h1aNH2b+
NWsI0+etFFbEWrmHZptbgPn34P3khB1K26NADVaRIBVeifuM0dbGvLWc6t27QQPdGYdnFY7BQlBv
k9vNdyx7w815nz8juybkMVtq7FCvbK8uEnBTcgMgNKVg5mSC1Enoewkp1kzMUUf0mlVuEcu/jHu2
f0p0eAN3xV5f4up+soujOrWuradmZ3uirYXzYrApagUHMqtjr+AhXJx7MuQCv9UPRU7ouidV/q36
Q/C4OpRqizjiKzulLhUoHmAUGMEQOd+ICoy71HOiK4MqnCmt2vI34cV9Cd5A8Hlfm6/COseor0Sq
26t4f8M8un7efc/RsF1xULiz/RoRAoGBAPvyQRyts6xpvDnanBLQa7b1Qf8oatYIcCcC7JlU+DZX
wD5qroyE5O7xStnSjqX5D6Lc7RbINkAuNGCofJzzynl5tP9j0WREueT1nq/YUW7Xn+Pd0fD6Fgb4
Js2vdRybH+vG4mv4gMxnS/gY+9jR7HL3GJRRQMMM5zWKY4LvrVADAoGBAO+W46I0/X5WCWellMod
Pa0M9OY3a8pJyP//JzblYykgw5nWWPHZEEOxV4VGFP0Pz4i6kpq/psWbCNLsh9k7EsqWLpeE7wsW
uXQj5LruIupL9/notboifL4zIOQcvHNs25iya+yURISYcVhmlqHHofX7ePfQR5sg1e1ZvethyR4H
AoGBAOH1ZhIrc14pQmf8uUdiZ4iiM/t8qzykOrmyNLJb83UBhGg2U6+xLIVkIMZ0wfz2/+AIFhb9
nzI2fkFGOuSk/S2vSvZV9qDfxn0jEJwS/Q3VExBRjA18ra64dky4lOb/9UQHjmBZcmJgLlEnTxAp
Tc/Z7tBugw+sDd0F7bOr85szAoGAOOBzLaCyxPkbxnUye0Cx0ZEP2k8x0ZXul4c1Af02qx7SEIUo
HFHRYKCLDGJ0vRaxx92yy/XPW33QfHIWVeWGMn2wldvC+7jrUbzroczCkShzt+ocqhFh160/k6eW
vTgMcZV5tXIFSgz+a2P/Qmyn8ENAlmPle9gxsOTrByPxoKUCgYEA1raYnqI9nKWkZYMrEOHx7Sy3
xCaKFSoc4nBxjJvZsSJ2aH6fJfMksPTisbYdSaXkGrb1fN2E7HxM1LsnbCyvXZsbMUV0zkk0Tzum
qDVW03gO4AvOD9Ix5gdebdq8le0xfMUzDvAIG1ypM+oMdZ122bI/rsOpLkZ4EtmixFxJbpk=""",
"""\
MIIEowIBAAKCAQEAs1+yp3NsF9qTybuupbt3cyBD+rEWUB+c3veK+TLTTu/hKrULCg6AaCXObv49
45xca0FxXc1/hbr7JinarjngmXj8Slr7UlTkbYKar9aGo3oMkMzbamQC4hBlp0fvH95f+A4M0iyM
RLGgcvZdk5/n0aXGOrlJ0maNFg5qgJcm38i5eRiItPJzTvnktYFcAbKV9IV3C8B8H2soubaJv0JF
nyPPA/pZDsK5/RNg+YRIflXKWe4dNH4/gt/3FwykQ7qdaoSpfoFS4WYCBPxJVcwzTfkwnAw7V+Lb
qxpBn0qJTz0sB6IIQWmOL5IhKd2isZVN9H2M+72vU+UDeCPrDYDbjQIDAQABAoIBAGBoVwVZLAfG
GxiaH0xEbfcaqG7dLzjxRMcyFSfLAXezxjnGBKDrGmjfqQxO6cSkDag4DE52XMvrq4DfjgGGagkS
1cbBD8M4jW2ufKV1j/fdaVOKR4PvLP2EAp7eMs/WHY6dPpbYCqwBLFOdxr3JfDdZ+ikl3V+QbtQj
+2oR03sC6HkpRiFJzrwatyKy3pq5CQkrO8fmzx+MtSOl4crwuX9cLw1K/6Zr0hSMP4LNc85WcH8h
7Fop2d405pQhy+dnBY19PQ0ODrv+wYXvWHClKy1U533sdqi8WcyCU2tu0MiWa5+kf/EB1J8LHi5X
Fyaut7pTU9766zBwmlVAvyeOfKECgYEA5lvwwcOop3oyPu9YevbX7gcEh5wqffnDIqFJbEbqDXd3
eSiGZfEuCcBhTQLgmX3RDMw9bbouisBEF+6DxrBDSdPQvpL/1j6/UscaecnNGPdIi9NkB+swtlOz
G4SRGx6nv+AY6y3cG11QO8q3jEXj8hzapVX7vFodt9FNor/kRTMCgYEAx1bvne8Fa7zBsYmefcDI
msWHaUdy8KuHqaWSz1IvXw0tXUnofSJ/Z51l8Lx2DVbX5Gj8IPEM/NsnYM7RiwFkRyvu+L25rswR
C2vO3kHELVU8YeZWxL4D0TQUpUcSEQzj4kFZjmnhey/8B32TtC00mOJP8vfr2pb3pk+Z9Pu03D8C
gYAreCgLeG+IAxAePi41LgV7PknFiVufYBVJoKPpUcxy9BtQeqw56nQklPAHh0Z40Hw1bQkefqav
ui5fUbv+L17TPKxEehrbBAY4iafeWY1ha7B96ksTD3emwE6pH6/+LR+8nn41SvchFs/AKLXQO5QT
KQy9bGdPmLXI7S84Sfu6bwKBgHxQjDjbQm8xFT6KC6xzGOfkvhD6/QR4hK9Y0di3cVF+30ape/Lm
G7xbnaJnddvVx+frTSmiCq56YfFuqaFd6dK05GB9uZn4K70Kq8VSEG0RFgob4wrpUWobZ7C3RN4b
QtbsWFSHVZZEk5F8UCvycTXTFXb6BD2bHrC6PdJZUy5zAoGBAIMmwTxxn5jO3l2CZxFC1+HzCh7w
Z3tq2p8JYqoLQQqVivAHfn5Lieh9ktxvWjQyZiUcEoFcWeikhwTwRmrwRPsylh15M4tfGbrYBEvN
+RXJuNVLt+ugJcbla7ghZnb1gkgxBWEVl3cW00eP0joi9kVcOyTEOLYH6fuDNso79KBz""",
"""\
MIIEpgIBAAKCAQEArnEcMtv09DktcSvk7t+RQMJqwAShxLPUfdMLsixahN1UU1VNIBY5sLBbKinS
5ixxzGTbDI9SKcM/ow7zN7KG8NEcpx3hTR45A4rJHvajeqnAbhucEcgnCu39QnGue03HW9BEJ5TM
6awpdrkUtpLoJviP8/8ClrNfQN8My10LcgsfFoQqxMo9YU5sj+kSm6/U3CS5Nuk3vxD5tabmBCBg
9rQ1komuE1Yet42NPmHdxjwC9npW01+uDoBrxmYaz1zJNNUiVk+2cwlsa1grvPU1UCBf4x3hNQC+
ZD3jGndnfcIUcrb0grsL85icFoXf/WEKjcKhGOUaVsypimCDyVkDDwIDAQABAoIBAQCtvpUihvgs
hAKh1OFZlq26/amLhVGGdMKxbBIbLZge+7+wnKaLzfc55/11OmEHxr61oMKYeOuSExmAFDTlhdhn
ZTAPt3Ae+no47/Ov9mIPm6HBSZiiEWPpu+7jTg1GXMqyxPYNImUSXNqTmHZr/lhh8HKYyKbQaOn3
1/GLYCo1M/6rgaftuJIl+uXKd3Sxy0fco7mGnqVn5+5MWibkIdZfqeVVImFcJSW9T+T5AnhihS/R
DXy0a+oX8fw06eTclM4GcOJVCjrXBH3kGiFLH//g07nhQVTHRuIPhB1cO+t1ByjX2S8zPpSuCctq
gtIe3+H6q5oIDcsy0dpoKPghTajhAoGBAOG4pxJ6X8RDXOHoqT9sZ5YCJUWpLXn4n47QVNdKW6hI
2aoveEjHxKKXJuA3EEeZA+Uu5TkgbsBv7mbgtAoZFbcoQEoNCgK5lAj/tjJXLv36RgOXuJYZivD9
rUzhbjiWvj1p2k9nQlgB7h321lLBgwhNsazKNpcX6/12WkWnAB+xAoGBAMXXgMi978AgymX5fWzT
rN/KKLd1nvxuxbKeKQ6ucHE/hssA5Zgk4ong9l09EPDfsoI5wDXgrvHVAXBXVtPq0fkO20FMaVct
27raLahp9C8yuvKPnaKD97B8r/dWsyV+eaREGAGUUiGx8QyapKyDD5byHOXIN5jBMXs9N91vfL6/
AoGBAKh3+yqNb4C6jk6GKhwOOtn5S/xMIocQi3Y6A7iT5QkbJmog9/PKNfbsPbXHIz1s9T1O3QLg
NAkpAZSDTZzj0BNd1W3vgXM7M0PsJv43l/kznKH90WUmN09a5sek0XEnAWIw6SGufhPVjPWMT7aA
e93srxm56zimQBpzBTlLRYphAoGBAJogeUPqNI0I/qTS6NOPVG5Dn9TM3T7rTTkJ3hKB8zdGtkwQ
Ns2AbrvbdhLNMBV3MCojs4pFsATWXHiYkhwmI85TtJv6W1Z/c17t+gPqB0F91AaDu9qP1La5bJzT
/lyHW1yNb+ZLFnEJnzCiiQecUtjVZY3dnPJ0D4hi+NKZuCUhAoGBAIvPIQPkqh09nNJpS5xoiPsM
4wAgmqBb1bTay+WGPqtjcOz3VcVmFX7EwTLgClTkuOXygzJno5FIx9JKb75Cd5KKvgCFXxmYt79N
vaYGagCA9BzT/93ejzuRTPpkbFBUL3AwmMyD1/DIzkADksfzhhKtB5QACkT00s0yzm4rVMwG""",
"""\
MIIEowIBAAKCAQEA0XrUDXtebDNoCNiqAUY3wizHGPmKeuMUduYeIpA+26OIT9Ougne7RYmJ6uQz
2NWuMkZhOxpuQXLMShsdjzx/YgAt/Ap7lZZMiorK5vRIsVuqI279nW8zovyGz043/20pRQy6csIA
z94mBWVpS7pjOBQ4fV0s4LxLZOxYvaSB5JsZAFjJk/40+EBGN51aLmiDfA5KUZLqpiL7eaKl14Tc
UH3Vwg4pn2DZtBvrJ5QSxtP2fOVf60U8MqR6g9xOPgxyhflcmqyPdFRpsaVTR6Rs211qkk3U+UP6
+xiWkiB/eEmw6JUnfDdLunjGKy2uYVXqyzMre8+4McmzYi7QyXLNGwIDAQABAoIBAQDAWoxrkNRE
gPPP47xADU1YFSwBh+scKnZ5M5eKX3AI2WJrAtLk5LLnCIPHWCMvwg8CBVR1JDEIEjT6+2kqRQAn
akjPfoS6+FdyhD4K01gI3EYf4WQq85iz2jSkGYwcFQ3nZOe0Rubd+XxqShPlQNKpBRBWNX/nIaAN
nWVjRrMryYJe7oycr3UF594RpBIo1DLFuIZOqttL+vy6MB+GzImEnJDYQg8vIpcRrDOt689sYFC8
7RPfK+ScWfxcU4gIfQZeIN4IqNANivXj5QIs/1uVxCXBBX9s1PPhg0HpPItTIi/r8iElyABHhfmv
JCQ7YqMfcfBOyuJuzRpG3OAaZ4IJAoGBAOlf4DtLl6TTSoBc6aLbzcQjQXyvzk35M9NtSbtP5uFh
UODa+tTubfLVK3MEPCf7PcLsuGeP649qpWNrkAebXDzibMWtz5O5y9uw11eHbxUeukFaVldVHHRt
b9lTKXTdZRfzm+chSyLQ3XikIwpRXqLK9rfir5zv7z8au1xo1BI1AoGBAOXJ6MPNE9zeFtssTjEK
x2HBaIQB7a4XjOY79nRvetLyPyLeGrJjcT554o6qpcWfi+KSGn75C9HQuAQZfmhRgmubwCn//V/S
u/ZPrZJyp/fGcIybmAM9fMJtE80u+gROaJbwomXHG+XNx8KgToHYbB5o0eLiH3EgBOmuM2yE5kwP
AoGAD39OZKGgcFGXoO6KlUYDZALzVlRWXtctmdyoCMhFjLHprQTdo0YyBu4g9IJTfFQyxb7yf+4O
tndehDugVOD8Pw7KKlZgcm7kGrKjmixkNALWW4CkOyhru0+JHeVn21rYW77Rm4eadbVo/5nmucit
gCH6QDvNbZ6BRK+BwaE0dAECgYBSDn8TZKliJuDMlY66jpnSe8mB0lp436oOEX2Z6LFYoO8Q2XV5
HG+1GrtfrOqTnrzKRNg3XWHuI/WCaUQtpmXHXZAKr4JgdJVwiNV3xX/byD4qx+lJxuxFVcRLcioP
3ZwVwoqLg8Wfk5NxGePPFGTPmyjQN2V49TEr7WwppW/D2wKBgFpAUk1vBM6kL37QMPLYXJgpk2ff
IDjChskWlUaR3yWTZaNoXLTMG/6SJwdAAr/riqnwAWox7Wv2UR5cBYsJe5vJK++6KthF2umNnzGg
Ymi6HkoIxiR2jmr56kDPeInqk6AB4vhqSn9PtLGtQyXp+0dkfeiE5qz36QX/EQTPULcd""",
]

TEST_KEYS_2048 = [
    mixminion.Crypto.pk_decode_private_key(base64.decodestring(s))
    for s in TEST_KEYS_2048 ]
del s
