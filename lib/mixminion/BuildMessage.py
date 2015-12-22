# Copyright 2002-2011 Nick Mathewson.  See LICENSE for licensing information.

"""mixminion.BuildMessage

   Code to construct messages and reply blocks, and to decode received
   message payloads."""

import logging
import operator
import sys
import types

import mixminion.Crypto as Crypto
import mixminion.Fragments
from mixminion.Packet import *
from mixminion.Common import MixError, MixFatalError, STATUS, UIError, \
     formatBase64
import mixminion.Packet
import mixminion._minionlib

if sys.version_info[:3] < (2,2,0):
    import mixminion._zlibutil as zlibutil

__all__ = ['buildForwardPacket', 'buildEncryptedForwardPacket',
           'buildReplyPacket', 'buildReplyBlock', 'checkPathLength',
           'encodeMessage', 'decodePayload', 'getNPacketsToEncode' ]


log = logging.getLogger(__name__)


def getNPacketsToEncode(message, overhead, uncompressedFragmentPrefix=""):
    """Return the number of packets that would be needed to encode 'message'.
       Arguments are as for encodeMessage.
    """
    assert overhead in (0, ENC_FWD_OVERHEAD)
    compressedLen = len(compressData(message))

    paddingLen = PAYLOAD_LEN - SINGLETON_PAYLOAD_OVERHEAD - overhead - compressedLen
    if paddingLen >= 0:
        return 1

    if uncompressedFragmentPrefix:
        compressedLen += len(uncompressedFragmentPrefix)

    p = mixminion.Fragments.FragmentationParams(compressedLen, overhead)

    return p.n * p.nChunks

def encodeMessage(message, overhead, uncompressedFragmentPrefix="",
                  paddingPRNG=None):
    """Given a message, compress it, fragment it into individual payloads,
       and add extra fields (size, hash, etc) as appropriate.  Return a list
       of strings, each of which is a message payload suitable for use in
       build*Message.

              message: the initial message
              overhead: number of bytes to omit from each payload,
                        given the type ofthe message encoding.
                        (0 or ENC_FWD_OVERHEAD)
              uncompressedFragmentPrefix: If we fragment the message,
                we add this string to the message after compression but
                before whitening and fragmentation.
              paddingPRNG: generator for padding.

       Note: If multiple strings are returned, be sure to shuffle them
       before transmitting them to the network.
    """
    assert overhead in (0, ENC_FWD_OVERHEAD)
    if paddingPRNG is None:
        paddingPRNG = Crypto.getCommonPRNG()
    origLength = len(message)
    payload = compressData(message)
    length = len(payload)

    if length > 1024 and length*20 <= origLength:
        log.warn("Message is very compressible and will look like a zlib bomb")

    paddingLen = PAYLOAD_LEN - SINGLETON_PAYLOAD_OVERHEAD - overhead - length

    # If the compressed payload fits in 28K, we're set.
    if paddingLen >= 0:
        # We pad the payload, and construct a new SingletonPayload,
        # including this payload's size and checksum.
        payload += paddingPRNG.getBytes(paddingLen)
        p = SingletonPayload(length, None, payload)
        p.computeHash()
        return [ p.pack() ]

    # Okay, we need to fragment the message.  First, add the prefix if needed.
    if uncompressedFragmentPrefix:
        payload = uncompressedFragmentPrefix+payload
    # Now generate a message ID
    messageid = Crypto.getCommonPRNG().getBytes(FRAGMENT_MESSAGEID_LEN)
    # Figure out how many chunks to divide it into...
    p = mixminion.Fragments.FragmentationParams(len(payload), overhead)
    # ... fragment the payload into chunks...
    rawFragments = p.getFragments(payload)
    fragments = []
    # ... and annotate each chunk with appropriate payload header info.
    for i in xrange(len(rawFragments)):
        pyld = FragmentPayload(i, None, messageid, p.length, rawFragments[i])
        pyld.computeHash()
        fragments.append(pyld.pack())
        rawFragments[i] = None
    return fragments

def buildRandomPayload(paddingPRNG=None):
    """Return a new random payload, suitable for use in a DROP packet."""
    if not paddingPRNG:
        paddingPRNG = Crypto.getCommonPRNG()
    return paddingPRNG.getBytes(PAYLOAD_LEN)

def buildForwardPacket(payload, exitType, exitInfo, path1, path2,
                       paddingPRNG=None, suppressTag=0):
    """Construct a forward message.
            payload: The payload to deliver.  Must be exactly 28K.  If the
                  payload is None, 28K of random data is sent.
            exitType: The routing type for the final node. (2 bytes, >=0x100)
            exitInfo: The routing info for the final node, not including tag.
            path1: Sequence of ServerInfo objects for the first leg of the path
            path2: Sequence of ServerInfo objects for the 2nd leg of the path
            paddingPRNG: random number generator used to generate padding.
                  If None, a new PRNG is initialized.
            suppressTag: if true, do not include a decodind handle in the
                  routingInfo for this packet.

        Neither path1 nor path2 may be empty.  If one is, MixError is raised.
    """
    if paddingPRNG is None:
        paddingPRNG = Crypto.getCommonPRNG()
    if not path1:
        raise MixError("First leg of path is empty")
    if not path2:
        raise MixError("Second leg of path is empty")

    assert len(payload) == PAYLOAD_LEN

    log.trace("  Building packet with path %s:%s; delivering to %04x:%r",
                   ",".join([s.getNickname() for s in path1]),
                   ",".join([s.getNickname() for s in path2]),
                   exitType, exitInfo)

    # Choose a random decoding tag.
    if not suppressTag:
        tag = _getRandomTag(paddingPRNG)
        exitInfo = tag + exitInfo
    return _buildPacket(payload, exitType, exitInfo, path1, path2,
                        paddingPRNG,suppressTag=suppressTag)


def buildEncryptedForwardPacket(payload, exitType, exitInfo, path1, path2,
                                 key, paddingPRNG=None, secretRNG=None):
    """Construct a forward message encrypted with the public key of a
       given user.
            payload: The payload to deliver.  Must be 28K-42b long.
            exitType: The routing type for the final node. (2 bytes, >=0x100)
            exitInfo: The routing info for the final node, not including tag.
            path1: Sequence of ServerInfo objects for the first leg of the path
            path2: Sequence of ServerInfo objects for the 2nd leg of the path
            key: Public key of this message's recipient.
            paddingPRNG: random number generator used to generate padding.
                  If None, a new PRNG is initialized.
    """
    if paddingPRNG is None:
        paddingPRNG = Crypto.getCommonPRNG()
    if secretRNG is None: secretRNG = paddingPRNG

    log.debug("Encoding encrypted forward message for %s-byte payload",
                   len(payload))
    log.debug("  Using path %s/%s",
                   [s.getNickname() for s in path1],
                   [s.getNickname() for s in path2])
    log.debug("  Delivering to %04x:%r", exitType, exitInfo)

    # (For encrypted-forward messages, we have overhead for OAEP padding
    #   and the session  key, but we save 20 bytes by spilling into the tag.)
    assert len(payload) == PAYLOAD_LEN - ENC_FWD_OVERHEAD

    # Generate the session key, and prepend it to the payload.
    sessionKey = secretRNG.getBytes(SECRET_LEN)
    payload = sessionKey+payload

    # We'll encrypt the first part of the new payload with RSA, and the
    # second half with Lioness, based on the session key.
    rsaDataLen = key.get_modulus_bytes()-OAEP_OVERHEAD
    rsaPart = payload[:rsaDataLen]
    lionessPart = payload[rsaDataLen:]

    # RSA encryption: To avoid leaking information about our RSA modulus,
    # we keep trying to encrypt until the MSBit of our encrypted value is
    # zero.
    while 1:
        encrypted = Crypto.pk_encrypt(rsaPart, key)
        if not (ord(encrypted[0]) & 0x80):
            break
    # Lioness encryption.
    k= Crypto.Keyset(sessionKey).getLionessKeys(Crypto.END_TO_END_ENCRYPT_MODE)
    lionessPart = Crypto.lioness_encrypt(lionessPart, k)

    # Now we re-divide the payload into the part that goes into the tag, and
    # the 28K of the payload proper...
    payload = encrypted + lionessPart
    tag = payload[:TAG_LEN]
    payload = payload[TAG_LEN:]
    exitInfo = tag + exitInfo
    assert len(payload) == 28*1024

    # And now, we can finally build the message.
    return _buildPacket(payload, exitType, exitInfo, path1, path2,paddingPRNG)

def buildReplyPacket(payload, path1, replyBlock, paddingPRNG=None):
    """Build a message using a reply block.  'path1' is a sequence of
       ServerInfo for the nodes on the first leg of the path.  'payload'
       must be exactly 28K long.
    """
    if paddingPRNG is None:
        paddingPRNG = Crypto.getCommonPRNG()

    log.debug("Encoding reply message for %s-byte payload",
                   len(payload))
    log.debug("  Using path %s/??",[s.getNickname() for s in path1])

    assert len(payload) == PAYLOAD_LEN

    # Encrypt the payload so that it won't appear as plaintext to the
    #  crossover note.  (We use 'decrypt' so that the message recipient can
    #  simply use 'encrypt' to reverse _all_ the steps of the reply path.)
    k = Crypto.Keyset(replyBlock.encryptionKey).getLionessKeys(
                         Crypto.PAYLOAD_ENCRYPT_MODE)
    payload = Crypto.lioness_decrypt(payload, k)

    return _buildPacket(payload, None, None,
                         path1=path1, path2=replyBlock)

def _buildReplyBlockImpl(path, exitType, exitInfo, expiryTime=0,
                         secretPRNG=None, tag=None):
    """Helper function: makes a reply block, given a tag and a PRNG to
       generate secrets. Returns a 3-tuple containing (1) a
       newly-constructed reply block, (2) a list of secrets used to
       make it, (3) a tag.

              path: A list of ServerInfo
              exitType: Routing type to use for the final node
              exitInfo: Routing info for the final node, not including tag.
              expiryTime: The time at which this block should expire.
              secretPRNG: A PRNG to use for generating secrets.  If not
                 provided, uses an AES counter-mode stream seeded from our
                 entropy source.  Note: the secrets are generated so that they
                 will be used to encrypt the message in reverse order.
              tag: If provided, a 159-bit tag.  If not provided, a new one
                 is generated.
       """
    if secretPRNG is None:
        secretPRNG = Crypto.getCommonPRNG()
    if expiryTime is None:
        # XXXX This is dangerous, and should go away; the user should
        # XXXX *always* specify an expiry time.
        log.warn("Inferring expiry time for reply block")
        expiryTime = min([s.getValidUntil() for s in path])

    checkPathLength(None, path, exitType, exitInfo, explicitSwap=0)

    log.debug("Building reply block for path %s",
                   [s.getNickname() for s in path])
    log.debug("  Delivering to %04x:%r", exitType, exitInfo)

    # The message is encrypted first by the end-to-end key, then by
    # each of the path keys in order. We need to reverse these steps, so we
    # generate the path keys back-to-front, followed by the end-to-end key.
    secrets = [ secretPRNG.getBytes(SECRET_LEN) for _ in range(len(path)+1) ]
    headerSecrets = secrets[:-1]
    headerSecrets.reverse()
    sharedKey = secrets[-1]

    # (This will go away when we deprecate 'stateful' reply blocks
    if tag is None:
        tag = _getRandomTag(secretPRNG)

    header = _buildHeader(path, headerSecrets, exitType, tag+exitInfo,
                          paddingPRNG=Crypto.getCommonPRNG())

    return ReplyBlock(header, expiryTime,
                     SWAP_FWD_HOST_TYPE,
                     path[0].getMMTPHostInfo().pack(), sharedKey), secrets, tag

# Maybe we shouldn't even allow this to be called with userKey==None.
def buildReplyBlock(path, exitType, exitInfo, userKey,
                    expiryTime=None, secretRNG=None):
    """Construct a 'state-carrying' reply block that does not require the
       reply-message recipient to remember a list of secrets.
       Instead, all secrets are generated from an AES counter-mode
       stream, and the seed for the stream is stored in the 'tag'
       field of the final block's routing info.   (See the spec for more
       info).

               path: a list of ServerInfo objects
               exitType,exitInfo: The address to deliver the final message.
               userKey: a string used to encrypt the seed.

       NOTE: We used to allow another kind of 'non-state-carrying' reply
       block that stored its secrets on disk, and used an arbitrary tag to
       determine which set of secrets to use.
       """
    if secretRNG is None:
        secretRNG = Crypto.getCommonPRNG()

    # We need to pick the seed to generate our keys.  To make the decoding
    # step a little faster, we find a seed such that H(seed|userKey|"Validate")
    # ends with 0.  This way, we can detect whether we really have a reply
    # message with 99.6% probability.  (Otherwise, we'd need to repeatedly
    # lioness-decrypt the payload in order to see whether the message was
    # a reply.)
    while 1:
        seed = _getRandomTag(secretRNG)
        if Crypto.sha1(seed+userKey+"Validate")[-1] == '\x00':
            break

    prng = Crypto.AESCounterPRNG(Crypto.sha1(seed+userKey+"Generate")[:16])

    replyBlock, secrets, tag = _buildReplyBlockImpl(path, exitType, exitInfo,
                                                    expiryTime, prng, seed)
    STATUS.log("GENERATED_SURB", formatBase64(tag))
    return replyBlock

def checkPathLength(path1, path2, exitType, exitInfo, explicitSwap=0,
                    suppressTag=0):
    """Given two path legs (lists of servers), an exit type and an
       exitInfo, raise an error if we can't build a header with the
       provided legs.  If suppressTag is true, no decoding handle will
       be included.

       The leg "path1" may be null.
    """
    err = 0 # 0: no error. 1: 1st leg too big. 2: 1st leg okay, 2nd too big.
    if path1 is not None and path2 is not None:
        try:
            rt,ri = path1[-1].getRoutingFor(path2[0],swap=1)
            _getRouting(path1, rt, ri)
        except MixError:
            err = 1
    # Add a dummy tag as needed to last exitinfo.
    if (not suppressTag
        and exitInfo is not None):
        exitInfo += "X"*20
    else:
        exitInfo = ""
    if err == 0:
        try:
            if path2 and not isinstance(path2, ReplyBlock):
                try:
                    _getRouting(path2, exitType, exitInfo)
                except:
                    print ">>>>>",path2
                    raise
        except MixError:
            err = 2
    if err and not explicitSwap:
        raise UIError("Address and path will not fit in one header")
    elif err:
        raise UIError("Address and %s leg of path will not fit in one header"
                      % ["first", "second"][err-1])

#----------------------------------------------------------------------
# MESSAGE DECODING
def decodePayload(payload, tag, key=None, userKeys=(), retNym=None):
    """Given a 28K payload and a 20-byte decoding tag, attempt to decode the
       original message.  Returns either a SingletonPayload instance, a
       FragmentPayload instance, or None.

           key: an RSA key to decode encrypted forward messages, or None
           userKeys: a sequence of (name,key) tuples maping identity names to
                SURB keys. For backward compatibility, 'userKeys' may also be
                None (no SURBs known), a dict (from name to key), or a single
                key (implied identity is "").
           retNym: If present, and if the payload was a reply, we call
                retNym.append(pseudonym).  (For the default SURB identity,
                we append the empty string.)

       If we can successfully decrypt the payload, we return it.  If we
       might be able to decrypt the payload given more/different keys,
       we return None.  If the payload is corrupt, we raise MixError.
    """
    if userKeys is None:
        userKeys = []
    elif type(userKeys) is types.StringType:
        userKeys = [ ("", userKeys) ]
    elif type(userKeys) is types.DictType:
        userKeys = userKeys.items()

    if len(payload) != PAYLOAD_LEN:
        raise MixError("Wrong payload length")

    if len(tag) not in (0, TAG_LEN):
        raise MixError("Wrong tag length: %s"%len(tag))

    # If the payload already contains a valid checksum, it's a forward
    # message.
    if _checkPayload(payload):
        return parsePayload(payload)

    if not tag:
        return None

    # If H(tag|userKey|"Validate") ends with 0, then the message _might_
    # be a reply message using H(tag|userKey|"Generate") as the seed for
    # its master secrets.  (There's a 1-in-256 chance that it isn't.)
    for name,userKey in userKeys:
        if Crypto.sha1(tag+userKey+"Validate")[-1] == '\x00':
            try:
                p = _decodeStatelessReplyPayload(payload, tag, userKey)
                if name:
                    log.info("Decoded reply message to identity %r", name)
                if retNym is not None:
                    retNym.append(name)
                return p
            except MixError:
                pass

    # If we have an RSA key, and none of the above steps get us a good
    # payload, then we may as well try to decrypt the start of tag+key with
    # our RSA key.
    if key is not None:
        p = _decodeEncryptedForwardPayload(payload, tag, key)
        if p is not None:
            return p

    return None

def _decodeForwardPayload(payload):
    """Helper function: decode a non-encrypted forward payload. Return values
       are the same as decodePayload."""
    if not _checkPayload(payload):
        raise MixError("Hash doesn't match")

    return parsePayload(payload)

def _decodeEncryptedForwardPayload(payload, tag, key):
    """Helper function: decode an encrypted forward payload.  Return values
       are the same as decodePayload.
             payload: the payload to decode
             tag: the decoding tag
             key: the RSA key of the payload's recipient."""
    assert len(tag) == TAG_LEN
    assert len(payload) == PAYLOAD_LEN

    # Given an N-byte RSA key, the first N bytes of tag+payload will be
    # encrypted with RSA, and the rest with a lioness key given in the
    # first N.  Try decrypting...
    msg = tag+payload
    try:
        rsaPart = Crypto.pk_decrypt(msg[:key.get_modulus_bytes()], key)
    except Crypto.CryptoError:
        return None
    rest = msg[key.get_modulus_bytes():]

    k = Crypto.Keyset(rsaPart[:SECRET_LEN]).getLionessKeys(
        Crypto.END_TO_END_ENCRYPT_MODE)
    rest = rsaPart[SECRET_LEN:] + Crypto.lioness_decrypt(rest, k)

    # ... and then, check the checksum and continue.
    if not _checkPayload(rest):
        raise MixError("Invalid checksum on encrypted forward payload")

    return parsePayload(rest)

def _decodeReplyPayload(payload, secrets, check=0):
    """Helper function: decode a reply payload, given a known list of packet
         master secrets. If 'check' is true, then 'secrets' may be overlong.
         Return values are the same as decodePayload.
      [secrets must be in _reverse_ order]
    """
    # Reverse the 'decrypt' operations of the reply mixes, and the initial
    # 'decrypt' of the originating user...
    for sec in secrets:
        k = Crypto.Keyset(sec).getLionessKeys(Crypto.PAYLOAD_ENCRYPT_MODE)
        payload = Crypto.lioness_encrypt(payload, k)
        if check and _checkPayload(payload):
            return parsePayload(payload)

    # If 'check' is false, then we might still have a good payload.  If
    # 'check' is true, we don't.
    if check or not _checkPayload(payload):
        raise MixError("Invalid checksum on reply payload")

    return parsePayload(payload)

def _decodeStatelessReplyPayload(payload, tag, userKey):
    """Decode a (state-carrying) reply payload."""
    # Reconstruct the secrets we used to generate the reply block (possibly
    # too many)
    seed = Crypto.sha1(tag+userKey+"Generate")[:16]
    prng = Crypto.AESCounterPRNG(seed)
    secrets = [ prng.getBytes(SECRET_LEN) for _ in xrange(17) ]

    return _decodeReplyPayload(payload, secrets, check=1)

#----------------------------------------------------------------------
def _buildPacket(payload, exitType, exitInfo,
                 path1, path2, paddingPRNG=None, paranoia=0,
                 suppressTag=0):
    """Helper method to create a message.

    The following fields must be set:
       payload: the intended exit payload.  Must be 28K.
       (exitType, exitInfo): the routing type and info for the final
              node.  (Ignored for reply messages; 'exitInfo' should
              include the 20-byte decoding tag.)
       path1: a sequence of ServerInfo objects, one for each node on
          the first leg of the path.
       path2:
        EITHER
             a sequence of ServerInfo objects, one for each node
             on the second leg of the path.
         OR
             a ReplyBlock object.

    The following fields are optional:
       paddingPRNG: A pseudo-random number generator used to pad the headers.
         If not provided, we use a counter-mode AES stream seeded from our
         entropy source.

       paranoia: If this is false, we use the padding PRNG to generate
         header secrets too.  Otherwise, we read all of our header secrets
         from the true entropy source.
    """
    assert len(payload) == PAYLOAD_LEN
    reply = None
    if isinstance(path2, ReplyBlock):
        reply = path2
        path2 = None
    else:
        if len(exitInfo) < TAG_LEN and not suppressTag:
            raise MixError("Implausibly short exit info: %r"%exitInfo)
        if exitType < MIN_EXIT_TYPE and exitType != DROP_TYPE:
            raise MixError("Invalid exit type: %4x"%exitType)

    checkPathLength(path1, path2, exitType, exitInfo,
                    explicitSwap=(reply is None),
                    suppressTag=suppressTag)

    ### SETUP CODE: let's handle all the variant cases.

    # Set up the random number generators.
    if paddingPRNG is None:
        paddingPRNG = Crypto.getCommonPRNG()
    if paranoia:
        nHops = len(path1)
        if path2: nHops += len(path2)
        secretRNG = Crypto.getTrueRNG()
    else:
        secretRNG = paddingPRNG

    # Determine exit routing for path1.
    if reply:
        path1exittype = reply.routingType
        path1exitinfo = reply.routingInfo
    else:
        path1exittype, path1exitinfo = path1[-1].getRoutingFor(path2[0],swap=1)

    # Generate secrets for path1.
    secrets1 = [ secretRNG.getBytes(SECRET_LEN) for _ in path1 ]

    if path2:
        # Make secrets for header 2, and construct header 2.  We do this before
        # making header1 so that our rng won't be used for padding yet.
        secrets2 = [ secretRNG.getBytes(SECRET_LEN) for _ in range(len(path2))]
        header2 = _buildHeader(path2,secrets2,exitType,exitInfo,paddingPRNG)
    else:
        secrets2 = None
        header2 = reply.header

    # Construct header1.
    header1 = _buildHeader(path1,secrets1,path1exittype,path1exitinfo,
                           paddingPRNG)

    return _constructMessage(secrets1, secrets2, header1, header2, payload)

def _buildHeader(path,secrets,exitType,exitInfo,paddingPRNG):
    """Helper method to construct a single header.
           path: A sequence of serverinfo objects.
           secrets: A list of 16-byte strings to use as master-secrets for
               each of the subheaders.
           exitType: The routing type for the last node in the header
           exitInfo: The routing info for the last node in the header.
               (Must include 20-byte decoding tag, if any.)
           paddingPRNG: A pseudo-random number generator to generate padding
    """
    assert len(path) == len(secrets)

    for info in path:
        if not info.supportsPacketVersion():
            raise MixError("Server %s does not support any recognized packet format."%info.getNickname())

    routing, sizes, totalSize = _getRouting(path, exitType, exitInfo)
    if totalSize > HEADER_LEN:
        raise MixError("Path cannot fit in header")

    # headerKey[i]==the AES key object node i will use to decrypt the header
    headerKeys = [ Crypto.Keyset(secret).get(Crypto.HEADER_SECRET_MODE)
                       for secret in secrets ]

    # Length of padding needed for the header
    paddingLen = HEADER_LEN - totalSize

    # Calculate junk.
    #   junkSeen[i]==the junk that node i will see, before it does any
    #                encryption.   Note that junkSeen[0]=="", because node 0
    #                sees no junk.
    junkSeen = [""]
    for secret, headerKey, size in zip(secrets, headerKeys, sizes):
        # Here we're calculating the junk that node i+1 will see.
        #
        # Node i+1 sees the junk that node i saw, plus the junk that i appends,
        # all encrypted by i.

        prngKey = Crypto.Keyset(secret).get(Crypto.RANDOM_JUNK_MODE)

        # newJunk is the junk that node i will append. (It's as long as
        #   the data that i removes.)
        newJunk = Crypto.prng(prngKey,size)
        lastJunk = junkSeen[-1]
        nextJunk = lastJunk + newJunk

        # Before we encrypt the junk, we'll encrypt all the data, and
        # all the initial padding, but not the RSA-encrypted part.
        #    This is equal to - 256
        #                     + sum(size[current]....size[last])
        #                     + paddingLen
        #    This simplifies to:
        #startIdx = paddingLen - 256 + totalSize - len(lastJunk)
        startIdx = HEADER_LEN - ENC_SUBHEADER_LEN - len(lastJunk)
        nextJunk = Crypto.ctr_crypt(nextJunk, headerKey, startIdx)
        junkSeen.append(nextJunk)

    # We start with the padding.
    header = paddingPRNG.getBytes(paddingLen)

    # Now, we build the subheaders, iterating through the nodes backwards.
    for i in range(len(path)-1, -1, -1):
        rt, ri = routing[i]

        # Create a subheader object for this node, but don't fill in the
        # digest until we've constructed the rest of the header.
        subhead = Subheader(MAJOR_NO, MINOR_NO,
                            secrets[i],
                            None, #placeholder for as-yet-uncalculated digest
                            rt, ri)

        # Do we need to include some of the remaining header in the
        # RSA-encrypted portion?
        underflowLength = subhead.getUnderflowLength()
        if underflowLength > 0:
            underflow = header[:underflowLength]
            header = header[underflowLength:]
        else:
            underflow = ""

        # Do we need to spill some of the routing info out from the
        # RSA-encrypted portion?  If so, prepend it.
        header = subhead.getOverflow() + header

        # Encrypt the symmetrically encrypted part of the header
        header = Crypto.ctr_crypt(header, headerKeys[i])

        # What digest will the next server see?
        subhead.digest = Crypto.sha1(header+junkSeen[i])

        # Encrypt the subheader, plus whatever portion of the previous header
        # underflows, into 'esh'.
        pubkey = path[i].getPacketKey()
        rsaPart = subhead.pack() + underflow
        esh = Crypto.pk_encrypt(rsaPart, pubkey)

        # Concatenate the asymmetric and symmetric parts, to get the next
        # header.
        header = esh + header

    return header

def _constructMessage(secrets1, secrets2, header1, header2, payload):
    """Helper method: Builds a message, given both headers, all known
       secrets, and the padded payload.

       If using a reply block for header2, secrets2 should be null.
    """
    assert len(payload) == PAYLOAD_LEN
    assert len(header1) == len(header2) == HEADER_LEN

    if secrets2:
        # (Copy secrets2 so we don't reverse the original)
        secrets2 = secrets2[:]

        # If we're not using a reply block, encrypt the payload for
        # each key in the second path, in reverse order.
        secrets2.reverse()
        for secret in secrets2:
            ks = Crypto.Keyset(secret)
            key = ks.getLionessKeys(Crypto.PAYLOAD_ENCRYPT_MODE)
            payload = Crypto.lioness_encrypt(payload, key)

    # Encrypt header2 with a hash of the payload.
    key = Crypto.lioness_keys_from_payload(payload)
    header2 = Crypto.lioness_encrypt(header2, key)

    # Encrypt payload with a hash of header2.  Now tagging either will make
    # both unrecoverable.
    key = Crypto.lioness_keys_from_header(header2)
    payload = Crypto.lioness_encrypt(payload, key)

    # Copy secrets1 so we don't reverse the original.
    secrets1 = secrets1[:]

    # Now, encrypt header2 and the payload for each node in path1, reversed.
    secrets1.reverse()
    for secret in secrets1:
        ks = Crypto.Keyset(secret)
        hkey = ks.getLionessKeys(Crypto.HEADER_ENCRYPT_MODE)
        pkey = ks.getLionessKeys(Crypto.PAYLOAD_ENCRYPT_MODE)
        header2 = Crypto.lioness_encrypt(header2,hkey)
        payload = Crypto.lioness_encrypt(payload,pkey)

    return Packet(header1, header2, payload).pack()

#----------------------------------------------------------------------
# Payload-related helpers

def _getRandomTag(rng):
    "Helper: Return a 20-byte string with the MSB of byte 0 set to 0."
    b = ord(rng.getBytes(1)) & 0x7f
    return chr(b) + rng.getBytes(TAG_LEN-1)

def _checkPayload(payload):
    'Return true iff the hash on the given payload seems valid'
    if (ord(payload[0]) & 0x80):
        return payload[3:23] == Crypto.sha1(payload[23:])
    else:
        return payload[2:22] == Crypto.sha1(payload[22:])

def _getRouting(path, exitType, exitInfo):
    """Given a list of ServerInfo, and a final exitType and exitInfo,
       return a 3-tuple of:
           1) A list of routingtype/routinginfo tuples for the header
           2) The size (in bytes) added to the header in order to
              route to each of the nodes
           3) Minimum size (in bytes) needed for the header.

       Raises MixError if the routing info is too big to fit into a single
       header. """
    # Construct a list 'routing' of exitType, exitInfo.
    routing = []
    for i in xrange(len(path)-1):
        routing.append(path[i].getRoutingFor(path[i+1],swap=0))
    routing.append((exitType, exitInfo))

    # sizes[i] is number of bytes added to header for subheader i.
    sizes = [ len(ri)+OAEP_OVERHEAD+MIN_SUBHEADER_LEN for _, ri in routing]

    # totalSize is the total number of bytes needed for header
    totalSize = reduce(operator.add, sizes)
    if totalSize > HEADER_LEN:
        raise MixError("Routing info won't fit in header")

    padding = HEADER_LEN-totalSize
    # We can't underflow from the last header.  That means we *must* have
    # enough space to pad the last routinginfo out to a public key size.
    if padding+sizes[-1] < ENC_SUBHEADER_LEN:
        raise MixError("Routing info won't fit in header")

    return routing, sizes, totalSize

