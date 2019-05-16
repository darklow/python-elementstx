# Copyright (C) 2019 The python-elementstx developers
# Copyright (C) 2018 The python-bitcointx developers
# Copyright (C) 2012-2017 The python-bitcoinlib developers
#
# This file is part of python-elementstx.
#
# It is subject to the license terms in the LICENSE file found in the top-level
# directory of this distribution.
#
# No part of python-elementstx, including this file, may be copied, modified,
# propagated, or distributed except according to the terms contained in the
# LICENSE file.
#
# Some code in this file is a direct translation from C++ code
# from Elements Project (https://github.com/ElementsProject/elements)
# Original C++ code was Copyright (c) 2017-2018 The Elements Core developers
# Original C++ code was under MIT license.

# pylama:ignore=E501

import os
import abc
import hmac
import struct
import ctypes
import hashlib
from collections import namedtuple

from bitcointx import get_current_chain_params

from elementstx.core.secp256k1 import (
    _secp256k1, secp256k1_has_zkp, secp256k1_blind_context,
    SECP256K1_GENERATOR_SIZE, SECP256K1_PEDERSEN_COMMITMENT_SIZE,
    build_aligned_data_array
)

from bitcointx.core.key import (
    CKey, CKeyMixin, CPubKey
)

from bitcointx.core import (
    Uint256, MoneyRange, CoinTransactionIdentityMeta,
    bytes_for_repr, ReprOrStrMixin, b2x,
    CTxWitnessBase, CTxInWitnessBase, CTxOutWitnessBase,
    CTxInBase, CTxOutBase, COutPointBase, COutPoint, CMutableOutPoint,
    CTransactionBase,

    CTransaction, CTxIn, CTxOut, CTxWitness, CTxInWitness, CTxOutWitness,

    CMutableTransaction, CMutableTxIn, CMutableTxOut, CMutableTxWitness,
    CMutableTxInWitness, CMutableTxOutWitness,
)

from bitcointx.util import no_bool_use_as_property
from bitcointx.core.script import CScriptWitness, CScript
from bitcointx.core.sha256 import CSHA256
from bitcointx.core.serialize import (
    ImmutableSerializable, SerializationError,
    BytesSerializer, VectorSerializer,
    ser_read, MutableSerializableMeta,
    is_mut_inst, is_mut_cls
)

from .script import CElementsScript

# If this flag is set, the CTxIn including this COutPoint has a CAssetIssuance object.
OUTPOINT_ISSUANCE_FLAG = (1 << 31)
# If this flag is set, the CTxIn including this COutPoint is a peg-in input.
OUTPOINT_PEGIN_FLAG = (1 << 30)
# The inverse of the combination of the preceeding flags. Used to
# extract the original meaning of `n` as the index into the
# transaction's output array. */
OUTPOINT_INDEX_MASK = 0x3fffffff


class ElementsTransactionIdentityMeta(CoinTransactionIdentityMeta):
    @classmethod
    def _get_extra_classmap(cls):
        return {CScript: CElementsScript}


class ElementsMutableTransactionIdentityMeta(ElementsTransactionIdentityMeta,
                                             MutableSerializableMeta):
    ...


class WitnessSerializationError(SerializationError):
    pass


class TxInSerializationError(SerializationError):
    pass


def _check_inst_compatible(inst, imm_concrete_class):
    if not isinstance(inst, imm_concrete_class):
        raise ValueError(
            'incompatible class: expected instance of {}, got {}'
            .format(imm_concrete_class.__name__, inst.__class__.__name__))


class CConfidentialCommitmentBase(ImmutableSerializable):
    _explicitSize = None
    _prefixA = None
    _prefixB = None

    _committedSize = 33

    __slots__ = ['commitment']

    def __init__(self, commitment=b''):
        object.__setattr__(self, 'commitment', bytes(commitment))

    @classmethod
    def stream_deserialize(cls, f):
        version = ser_read(f, 1)[0]
        read_size = 0
        if version == 0:
            read_size = 0
        elif version == 1:
            read_size = cls._explicitSize
        elif version in (cls._prefixA, cls._prefixB):
            read_size = cls._committedSize
        else:
            raise WitnessSerializationError('Unrecognized serialization prefix')

        if read_size > 0:
            commitment = bytes([version]) + ser_read(f, read_size-1)
        else:
            commitment = b''

        return cls(commitment)

    def stream_serialize(self, f):
        if len(self.commitment):
            f.write(self.commitment)
        else:
            f.write(bytes([0]))

    @no_bool_use_as_property
    def is_null(self):
        return not len(self.commitment)

    @no_bool_use_as_property
    def is_explicit(self):
        return (len(self.commitment) == self._explicitSize
                and self.commitment[0] == 1)

    @no_bool_use_as_property
    def is_commitment(self):
        return (len(self.commitment) == self._committedSize
                and self.commitment[0] in (self._prefixA, self._prefixB))

    @no_bool_use_as_property
    def is_valid(self):
        return self.is_null() or self.is_explicit() or self.is_commitment()

    def _get_explicit(self):
        raise NotImplementedError

    def __str__(self):
        if self.is_explicit():
            v = str(self._get_explicit())
        else:
            v = 'CONFIDENTIAL'
        return "{}({})".format(self.__class__.__name__, v)

    def __repr__(self):
        if self.is_explicit():
            v = repr(self._get_explicit())
        else:
            v = bytes_for_repr(self.commitment)
        return "{}({})".format(self.__class__.__name__, v)


class CAsset(Uint256):
    def __repr__(self):
        return "{}('{}')".format(self.__class__.__name__, self.to_hex())

    def to_commitment(self):
        gen = ctypes.create_string_buffer(64)
        res = _secp256k1.secp256k1_generator_generate(
            secp256k1_blind_context, gen, self.data)
        if res != 1:
            raise ValueError('invalid asset data')
        result_commitment = ctypes.create_string_buffer(CConfidentialAsset._committedSize)
        ret = _secp256k1.secp256k1_generator_serialize(
            secp256k1_blind_context, result_commitment, gen)
        assert ret == 1
        return result_commitment.raw


class CConfidentialAsset(CConfidentialCommitmentBase):
    _explicitSize = 33
    _prefixA = 10
    _prefixB = 11

    def __init__(self, asset_or_commitment=b''):
        assert(isinstance(asset_or_commitment, (CAsset, bytes, bytearray)))
        if isinstance(asset_or_commitment, CAsset):
            commitment = bytes([1]) + asset_or_commitment.data
        else:
            commitment = asset_or_commitment
        super(CConfidentialAsset, self).__init__(commitment)

    @classmethod
    def from_asset(cls, asset):
        assert isinstance(asset, CAsset)
        return cls(asset)

    def to_asset(self):
        assert self.is_explicit()
        return CAsset(self.commitment[1:])

    def _get_explicit(self):
        return self.to_asset()


class CConfidentialValue(CConfidentialCommitmentBase):
    _explicitSize = 9
    _prefixA = 8
    _prefixB = 9

    def __init__(self, value_or_commitment=b''):
        assert isinstance(value_or_commitment, (int, bytes, bytearray))
        if isinstance(value_or_commitment, int):
            commitment = bytes([1]) + struct.pack(b">q", value_or_commitment)
        else:
            commitment = value_or_commitment
        super(CConfidentialValue, self).__init__(commitment)

    @classmethod
    def from_amount(cls, amount):
        assert isinstance(amount, int)
        return cls(amount)

    def to_amount(self):
        assert self.is_explicit()
        return struct.unpack(b">q", self.commitment[1:])[0]

    def _get_explicit(self):
        return self.to_amount()


class CConfidentialNonce(CConfidentialCommitmentBase):
    _explicitSize = 33
    _prefixA = 2
    _prefixB = 3

    def _get_explicit(self):
        return 'CONFIDENTIAL'

    def __repr__(self):
        v = "x('{}')".format(b2x(self.commitment))
        return "{}({})".format(self.__class__.__name__, v)


class CElementsOutPoint(COutPointBase,
                        metaclass=ElementsTransactionIdentityMeta):
    """Elements COutPoint"""
    __slots__ = []


class CElementsMutableOutPoint(CElementsOutPoint,
                               metaclass=ElementsMutableTransactionIdentityMeta):
    """A mutable Elements COutPoint"""
    __slots__ = []


class CElementsTxInWitness(ReprOrStrMixin, CTxInWitnessBase,
                           metaclass=ElementsTransactionIdentityMeta):
    """Witness data for a single transaction input of elements transaction"""
    __slots__ = ['scriptWitness',
                 'issuanceAmountRangeproof', 'inflationKeysRangeproof', 'pegin_witness']

    # put scriptWitness first for CTxInWitness(script_witness) to work
    # the same as with CBitcoinTxInWitness.
    def __init__(self, scriptWitness=CScriptWitness(),
                 issuanceAmountRangeproof=b'', inflationKeysRangeproof=b'',
                 pegin_witness=CScriptWitness()):
        assert isinstance(issuanceAmountRangeproof, (bytes, bytearray))
        assert isinstance(inflationKeysRangeproof, (bytes, bytearray))
        object.__setattr__(self, 'scriptWitness', scriptWitness)
        object.__setattr__(self, 'issuanceAmountRangeproof',
                           CElementsScript(issuanceAmountRangeproof))
        object.__setattr__(self, 'inflationKeysRangeproof',
                           CElementsScript(inflationKeysRangeproof))
        # Note that scriptWitness/pegin_witness naming convention mismatch
        # exists in reference client code, and is retained here.
        object.__setattr__(self, 'pegin_witness', pegin_witness)

    @no_bool_use_as_property
    def is_null(self):
        return (not len(self.issuanceAmountRangeproof)
                and not len(self.inflationKeysRangeproof)
                and self.scriptWitness.is_null()
                and self.pegin_witness.is_null())

    @classmethod
    def stream_deserialize(cls, f):
        issuanceAmountRangeproof = CElementsScript(BytesSerializer.stream_deserialize(f))
        inflationKeysRangeproof = CElementsScript(BytesSerializer.stream_deserialize(f))
        scriptWitness = CScriptWitness.stream_deserialize(f)
        pegin_witness = CScriptWitness.stream_deserialize(f)
        return cls(scriptWitness, issuanceAmountRangeproof, inflationKeysRangeproof,
                   pegin_witness)

    def stream_serialize(self, f):
        BytesSerializer.stream_serialize(self.issuanceAmountRangeproof, f)
        BytesSerializer.stream_serialize(self.inflationKeysRangeproof, f)
        self.scriptWitness.stream_serialize(f)
        self.pegin_witness.stream_serialize(f)

    @classmethod
    def from_txin_witness(cls, txin_witness):
        _check_inst_compatible(txin_witness,
                               cls._concrete_class.immutable.CTxInWitness)

        if not is_mut_cls(cls) and not is_mut_inst(txin_witness):
            return txin_witness

        return cls(scriptWitness=txin_witness.scriptWitness,
                   issuanceAmountRangeproof=txin_witness.issuanceAmountRangeproof,
                   inflationKeysRangeproof=txin_witness.inflationKeysRangeproof,
                   pegin_witness=txin_witness.pegin_witness)

    def _repr_or_str(self, strfn):
        if self.is_null():
            return "{}()".format(self.__class__.__name__)
        return "{}({}, {}, {}, {})".format(
            self.__class__.__name__,
            strfn(self.scriptWitness), bytes_for_repr(self.issuanceAmountRangeproof),
            bytes_for_repr(self.inflationKeysRangeproof), strfn(self.pegin_witness))


class CElementsMutableTxInWitness(CElementsTxInWitness,
                                  metaclass=ElementsMutableTransactionIdentityMeta):
    __slots__ = []


class CElementsTxOutWitness(CTxOutWitnessBase,
                            metaclass=ElementsTransactionIdentityMeta):
    """Witness data for a single transaction output of elements transaction"""
    __slots__ = ['surjectionproof', 'rangeproof']

    def __init__(self, surjectionproof=b'', rangeproof=b''):
        assert isinstance(surjectionproof, (bytes, bytearray))
        assert isinstance(rangeproof, (bytes, bytearray))
        object.__setattr__(self, 'surjectionproof', CElementsScript(surjectionproof))
        object.__setattr__(self, 'rangeproof', CElementsScript(rangeproof))

    @no_bool_use_as_property
    def is_null(self):
        return not len(self.surjectionproof) and not len(self.rangeproof)

    @classmethod
    def stream_deserialize(cls, f):
        surjectionproof = CElementsScript(BytesSerializer.stream_deserialize(f))
        rangeproof = CElementsScript(BytesSerializer.stream_deserialize(f))
        return cls(surjectionproof, rangeproof)

    def stream_serialize(self, f):
        BytesSerializer.stream_serialize(self.surjectionproof, f)
        BytesSerializer.stream_serialize(self.rangeproof, f)

    def get_rangeproof_info(self):
        if not secp256k1_has_zkp:
            raise RuntimeError('secp256k1-zkp library is not available. '
                               ' get_rangeproof_info is not functional.')

        exp = ctypes.c_int()
        mantissa = ctypes.c_int()
        value_min = ctypes.c_uint64()
        value_max = ctypes.c_uint64()
        result = _secp256k1.secp256k1_rangeproof_info(
            secp256k1_blind_context,
            ctypes.byref(exp), ctypes.byref(mantissa),
            ctypes.byref(value_min), ctypes.byref(value_max),
            self.rangeproof, len(self.rangeproof)
        )
        if result != 1:
            assert result == 0
            return None

        return ZKPRangeproofInfo(exp=exp.value, mantissa=mantissa.value,
                                 value_min=value_min.value, value_max=value_max.value)

    @classmethod
    def from_txout_witness(cls, txout_witness):
        _check_inst_compatible(txout_witness,
                               cls._concrete_class.immutable.CTxOutWitness)

        if not is_mut_cls(cls) and not is_mut_inst(txout_witness):
            return txout_witness

        return cls(surjectionproof=txout_witness.surjectionproof,
                   rangeproof=txout_witness.rangeproof)

    def __repr__(self):
        if self.is_null():
            return "{}()".format(self.__class__.__name__)
        return "{}({}, {})".format(
            self.__class__.__name__,
            bytes_for_repr(self.surjectionproof),
            bytes_for_repr(self.rangeproof))


class CElementsMutableTxOutWitness(CElementsTxOutWitness,
                                   metaclass=ElementsMutableTransactionIdentityMeta):
    __slots__ = []


class CElementsTxWitness(ReprOrStrMixin, CTxWitnessBase,
                         metaclass=ElementsTransactionIdentityMeta):

    __slots__ = ['vtxinwit', 'vtxoutwit']

    def __init__(self, vtxinwit=(), vtxoutwit=()):
        def process_wit(in_witlist, imm_w_cls, clone_f):
            witlist = []
            for w in in_witlist:
                _check_inst_compatible(w, imm_w_cls)
                if is_mut_inst(self) or is_mut_inst(w):
                    witlist.append(clone_f(w))
                else:
                    witlist.append(w)

            if not is_mut_inst(self):
                witlist = tuple(witlist)

            return witlist

        object.__setattr__(
            self, 'vtxinwit',
            process_wit(vtxinwit,
                        self._concrete_class.immutable.CTxInWitness,
                        self._concrete_class.CTxInWitness.from_txin_witness))
        object.__setattr__(
            self, 'vtxoutwit',
            process_wit(vtxoutwit,
                        self._concrete_class.immutable.CTxOutWitness,
                        self._concrete_class.CTxOutWitness.from_txout_witness))

    @no_bool_use_as_property
    def is_null(self):
        for n in range(len(self.vtxinwit)):
            if not self.vtxinwit[n].is_null():
                return False
        for n in range(len(self.vtxoutwit)):
            if not self.vtxoutwit[n].is_null():
                return False
        return True

    # NOTE: this cannot be a @classmethod like the others because we need to
    # know how many items to deserialize, which comes from len(vin)
    def stream_deserialize(self, f):
        vtxinwit = tuple(
            self._concrete_class.CTxInWitness.stream_deserialize(f)
            for dummy in range(len(self.vtxinwit)))
        vtxoutwit = tuple(
            self._concrete_class.CTxOutWitness.stream_deserialize(f)
            for dummy in range(len(self.vtxoutwit)))
        return self.__class__(vtxinwit, vtxoutwit)

    def stream_serialize(self, f):
        for i in range(len(self.vtxinwit)):
            self.vtxinwit[i].stream_serialize(f)
        for i in range(len(self.vtxoutwit)):
            self.vtxoutwit[i].stream_serialize(f)

    @classmethod
    def from_witness(cls, witness):
        _check_inst_compatible(witness,
                               cls._concrete_class.immutable.CTxWitness)

        if not is_mut_cls(cls) and not is_mut_inst(witness):
            return witness

        vtxinwit = (cls._concrete_class.CTxInWitness.from_txin_witness(w)
                    for w in witness.vtxinwit)
        vtxoutwit = (cls._concrete_class.CTxOutWitness.from_txout_witness(w)
                     for w in witness.vtxoutwit)
        return cls(vtxinwit, vtxoutwit)

    def _repr_or_str(self, strfn):
        return "%s([%s], [%s])" % (
            self.__class__.__name__,
            ','.join(strfn(w) for w in self.vtxinwit),
            ','.join(strfn(w) for w in self.vtxoutwit))


class CElementsMutableTxWitness(CElementsTxWitness,
                                metaclass=ElementsMutableTransactionIdentityMeta):
    __slots__ = []


class CAssetIssuance(ImmutableSerializable, ReprOrStrMixin):
    __slots__ = ['assetBlindingNonce', 'assetEntropy', 'nAmount', 'nInflationKeys']

    def __init__(self, assetBlindingNonce=Uint256(), assetEntropy=Uint256(),
                 nAmount=CConfidentialValue(), nInflationKeys=CConfidentialValue()):
        object.__setattr__(self, 'assetBlindingNonce', assetBlindingNonce)
        object.__setattr__(self, 'assetEntropy', assetEntropy)
        object.__setattr__(self, 'nAmount', nAmount)
        object.__setattr__(self, 'nInflationKeys', nInflationKeys)

    @no_bool_use_as_property
    def is_null(self):
        return self.nAmount.is_null() and self.nInflationKeys.is_null()

    @classmethod
    def stream_deserialize(cls, f):
        assetBlindingNonce = Uint256.stream_deserialize(f)
        assetEntropy = Uint256.stream_deserialize(f)
        nAmount = CConfidentialValue.stream_deserialize(f)
        nInflationKeys = CConfidentialValue.stream_deserialize(f)
        return cls(assetBlindingNonce, assetEntropy, nAmount, nInflationKeys)

    def stream_serialize(self, f):
        self.assetBlindingNonce.stream_serialize(f)
        self.assetEntropy.stream_serialize(f)
        self.nAmount.stream_serialize(f)
        self.nInflationKeys.stream_serialize(f)

    def _repr_or_str(self, strfn):
        r = []
        if self.assetBlindingNonce.to_int():
            r.append(bytes_for_repr(self.assetBlindingNonce.data))
        if self.assetEntropy.to_int():
            r.append(bytes_for_repr(self.assetEntropy.data))
        if not self.nAmount.is_null():
            r.append(strfn(self.nAmount))
        if not self.nInflationKeys.is_null():
            r.append(strfn(self.nInflationKeys))
        return 'CAssetIssuance({})'.format(', '.join(r))


class CElementsTxIn(ReprOrStrMixin, CTxInBase,
                    metaclass=ElementsTransactionIdentityMeta):
    """Immutable Elements CTxIn"""
    __slots__ = ['prevout', 'scriptSig', 'nSequence', 'assetIssuance', 'is_pegin']

    def __init__(self, prevout=None, scriptSig=CElementsScript(), nSequence=0xffffffff,
                 assetIssuance=CAssetIssuance(), is_pegin=False):
        if not isinstance(scriptSig, CElementsScript):
            assert isinstance(scriptSig, (bytes, bytearray))
            scriptSig = CElementsScript(scriptSig)
        super(CElementsTxIn, self).__init__(prevout, scriptSig, nSequence)
        object.__setattr__(self, 'assetIssuance', assetIssuance)
        object.__setattr__(self, 'is_pegin', is_pegin)

    @classmethod
    def stream_deserialize(cls, f):
        base = super(CElementsTxIn, cls).stream_deserialize(f)
        if base.prevout.n == 0xffffffff:
            # No asset issuance for Coinbase inputs.
            has_asset_issuance = False
            is_pegin = False
            prevout = base.prevout
        else:
            # The presence of the asset issuance object is indicated by
            # a bit set in the outpoint index field.
            has_asset_issuance = bool(base.prevout.n & OUTPOINT_ISSUANCE_FLAG)
            # The interpretation of this input as a peg-in is indicated by
            # a bit set in the outpoint index field.
            is_pegin = bool(base.prevout.n & OUTPOINT_PEGIN_FLAG)
            # The mode, if set, must be masked out of the outpoint so
            # that the in-memory index field retains its traditional
            # meaning of identifying the index into the output array
            # of the previous transaction.
            prevout = cls._concrete_class.COutPoint(
                base.prevout.hash, base.prevout.n & OUTPOINT_INDEX_MASK)

        if has_asset_issuance:
            assetIssuance = CAssetIssuance.stream_deserialize(f)
        else:
            assetIssuance = CAssetIssuance()

        return cls(prevout, base.scriptSig, base.nSequence, assetIssuance, is_pegin)

    def stream_serialize(self, f, for_sighash=False):
        if self.prevout.n == 0xffffffff:
            has_asset_issuance = False
            outpoint = self.prevout
        else:
            if self.prevout.n & ~OUTPOINT_INDEX_MASK:
                raise TxInSerializationError('High bits of prevout.n should not be set')

            has_asset_issuance = not self.assetIssuance.is_null()
            n = self.prevout.n & OUTPOINT_INDEX_MASK
            if not for_sighash:
                if has_asset_issuance:
                    n |= OUTPOINT_ISSUANCE_FLAG
                if self.is_pegin:
                    n |= OUTPOINT_PEGIN_FLAG
            outpoint = self._concrete_class.COutPoint(self.prevout.hash, n)

        self._concrete_class.COutPoint.stream_serialize(outpoint, f)
        BytesSerializer.stream_serialize(self.scriptSig, f)
        f.write(struct.pack(b"<I", self.nSequence))

        if has_asset_issuance:
            self.assetIssuance.stream_serialize(f)

    def _repr_or_str(self, strfn):
        return "%s(%s, %s, 0x%x, %s, is_pegin=%r)" % (
            self.__class__.__name__,
            strfn(self.prevout), repr(self.scriptSig),
            self.nSequence, strfn(self.assetIssuance),
            self.is_pegin)

    @classmethod
    def from_txin(cls, txin):
        _check_inst_compatible(txin, cls._concrete_class.immutable.CTxIn)

        if not is_mut_cls(cls) and not is_mut_inst(txin):
            return txin

        return cls(
            cls._concrete_class.COutPoint.from_outpoint(txin.prevout),
            txin.scriptSig, txin.nSequence, txin.assetIssuance, txin.is_pegin)


class CElementsMutableTxIn(CElementsTxIn,
                           metaclass=ElementsMutableTransactionIdentityMeta):
    """A mutable Elements CTxIn"""
    __slots__ = []


class CElementsTxOut(ReprOrStrMixin, CTxOutBase,
                     metaclass=ElementsTransactionIdentityMeta):
    """An output of an Elements transaction
    """
    __slots__ = ['nValue', 'scriptPubKey', 'nAsset', 'nNonce']

    # nValue and scriptPubKey is first to be compatible with
    # CTxOut(nValue, scriptPubKey) calls
    def __init__(self, nValue=CConfidentialValue(), scriptPubKey=CElementsScript(),
                 nAsset=CConfidentialAsset(), nNonce=CConfidentialNonce()):
        assert isinstance(nValue, CConfidentialValue)
        assert isinstance(nAsset, CConfidentialAsset)
        assert isinstance(nNonce, CConfidentialNonce)
        if not isinstance(scriptPubKey, CElementsScript):
            assert isinstance(scriptPubKey, (bytes, bytearray))
            scriptPubKey = CElementsScript(scriptPubKey)
        object.__setattr__(self, 'nAsset', nAsset)
        object.__setattr__(self, 'nValue', nValue)
        object.__setattr__(self, 'nNonce', nNonce)
        object.__setattr__(self, 'scriptPubKey', scriptPubKey)

    @classmethod
    def stream_deserialize(cls, f):
        nAsset = CConfidentialAsset.stream_deserialize(f)
        nValue = CConfidentialValue.stream_deserialize(f)
        nNonce = CConfidentialNonce.stream_deserialize(f)
        scriptPubKey = CElementsScript(BytesSerializer.stream_deserialize(f))
        return cls(nValue, scriptPubKey, nAsset, nNonce)

    def stream_serialize(self, f):
        self.nAsset.stream_serialize(f)
        self.nValue.stream_serialize(f)
        self.nNonce.stream_serialize(f)
        BytesSerializer.stream_serialize(self.scriptPubKey, f)

    @no_bool_use_as_property
    def is_null(self):
        return (self.nAsset.is_null() and self.nValue.is_null()
                and self.nNonce.is_null() and not len(self.scriptPubKey))

    @no_bool_use_as_property
    def is_fee(self):
        return (not len(self.scriptPubKey)
                and self.nValue.is_explicit()
                and self.nAsset.is_explicit())

    def _repr_or_str(self, strfn):
        return "{}({}, {}, {}, {})".format(
            self.__class__.__name__,
            strfn(self.nValue), repr(self.scriptPubKey), strfn(self.nAsset),
            strfn(self.nNonce))

    def unblind(self, blinding_key=None, rangeproof=None):
        """Unblinds a txout, given a key and a rangeproof.
        returns a tuple of (success, result)
        If success is True, result is BlindingInputDescriptor namedtuple.
        If success is False, result is a string describing the cause of failure"""
        return unblind_confidential_pair(
            blinding_key, self.nValue, self.nAsset, self.nNonce,
            self.scriptPubKey, rangeproof)

    @classmethod
    def from_txout(cls, txout):
        _check_inst_compatible(txout, cls._concrete_class.immutable.CTxOut)

        if not is_mut_cls(cls) and not is_mut_inst(txout):
            return txout

        return cls(txout.nValue, txout.scriptPubKey,
                   txout.nAsset, txout.nNonce)


class CElementsMutableTxOut(CElementsTxOut,
                            metaclass=ElementsMutableTransactionIdentityMeta):
    __slots__ = []


class CElementsTransaction(CTransactionBase,
                           metaclass=ElementsTransactionIdentityMeta):
    __slots__ = []

    @classmethod
    def stream_deserialize(cls, f):
        """Deserialize transaction

        This implementation corresponds to Elements's SerializeTransaction() and
        consensus behavior. Note that Elements's DecodeHexTx() also has the
        option to attempt deserializing as a non-witness transaction first,
        falling back to the consensus behavior if it fails. The difference lies
        in transactions which have zero inputs: they are invalid but may be
        (de)serialized anyway for the purpose of signing them and adding
        inputs. If the behavior of DecodeHexTx() is needed it could be added,
        but not here.
        """
        # FIXME can't assume f is seekable
        nVersion = struct.unpack(b"<i", ser_read(f, 4))[0]

        markerbyte = 0
        flagbyte = struct.unpack(b'B', ser_read(f, 1))[0]
        if markerbyte == 0 and flagbyte == 1:
            vin = VectorSerializer.stream_deserialize(cls._concrete_class.CTxIn, f)
            vout = VectorSerializer.stream_deserialize(cls._concrete_class.CTxOut, f)
            wit = cls._concrete_class.CTxWitness(
                tuple(cls._concrete_class.CTxInWitness()
                      for dummy in range(len(vin))),
                tuple(cls._concrete_class.CTxOutWitness()
                      for dummy in range(len(vout))))
            # Note: nLockTime goes before witness in Elements transactions
            nLockTime = struct.unpack(b"<I", ser_read(f, 4))[0]
            wit = wit.stream_deserialize(f)
            return cls(vin, vout, nLockTime, nVersion, wit)
        else:
            vin = VectorSerializer.stream_deserialize(cls._concrete_class.CTxIn, f)
            vout = VectorSerializer.stream_deserialize(cls._concrete_class.CTxOut, f)
            nLockTime = struct.unpack(b"<I", ser_read(f, 4))[0]
            return cls(vin, vout, nLockTime, nVersion)

    def stream_serialize(self, f, include_witness=True, for_sighash=False):
        f.write(struct.pack(b"<i", self.nVersion))
        if include_witness and not self.wit.is_null():
            assert(len(self.wit.vtxinwit) == 0 or len(self.wit.vtxinwit) == len(self.vin))
            assert(len(self.wit.vtxoutwit) == 0 or len(self.wit.vtxoutwit) == len(self.vout))
            f.write(b'\x01')  # Flag
            # no check of for_sighash, because standard sighash calls this without witnesses.
            VectorSerializer.stream_serialize(self._concrete_class.CTxIn, self.vin, f)
            VectorSerializer.stream_serialize(self._concrete_class.CTxOut, self.vout, f)
            # Note: nLockTime goes before witness in Elements transactions
            f.write(struct.pack(b"<I", self.nLockTime))
            self.wit.stream_serialize(f)
        else:
            if not for_sighash:
                f.write(b'\x00')  # Flag is needed in Elements
            VectorSerializer.stream_serialize(self._concrete_class.CTxIn, self.vin, f,
                                              inner_params={'for_sighash': for_sighash})
            VectorSerializer.stream_serialize(self._concrete_class.CTxOut, self.vout, f)
            f.write(struct.pack(b"<I", self.nLockTime))

    @property
    def num_issuances(self):
        numIssuances = 0
        for txin in self.vin:
            if not txin.assetIssuance.is_null():
                if not txin.assetIssuance.nAmount.is_null():
                    numIssuances += 1
                if not txin.assetIssuance.nInflationKeys.is_null():
                    numIssuances += 1

        return numIssuances


class CElementsMutableTransaction(CElementsTransaction,
                                  metaclass=ElementsMutableTransactionIdentityMeta):
    ...


def blind_transaction(tx, input_descriptors=(), output_pubkeys=(), # noqa
                      blind_issuance_asset_keys=(), blind_issuance_token_keys=(),
                      auxiliary_generators=(), _rand_func=os.urandom):

    """Blinds the transaction. Return a BlindingOrUnblindingResult"""

    if not isinstance(tx, CElementsTransaction):
        raise ValueError('can blind only Elements transactions')

    if not is_mut_inst(tx):
        raise ValueError('only mutable transaction can be blinded')

    # based on Elements Core's BlindTransaction() function from src/blind.cpp
    # as of commit 43f6cdbd3147d9af450b73c8b8b8936e3e4166df

    assert len(tx.vout) >= len(output_pubkeys)
    assert all(isinstance(p, CPubKey) for p in output_pubkeys)
    assert len(tx.vin) + tx.num_issuances >= len(blind_issuance_asset_keys)
    assert all(k is None or isinstance(k, CKey) for k in blind_issuance_asset_keys)
    assert len(tx.vin) + tx.num_issuances >= len(blind_issuance_token_keys)
    assert all(k is None or isinstance(k, CKey) for k in blind_issuance_token_keys)
    assert len(tx.vin) == len(input_descriptors)
    for idesc in input_descriptors:
        assert isinstance(idesc.blinding_factor, Uint256)
        assert isinstance(idesc.asset_blinding_factor, Uint256)
        assert isinstance(idesc.asset, CAsset)
        assert isinstance(idesc.amount, int)

    output_blinding_factors = [None for _ in range(len(tx.vout))]
    output_asset_blinding_factors = [None for _ in range(len(tx.vout))]

    def blinding_success(nSuccessfullyBlinded):
        for i, bf in enumerate(output_blinding_factors):
            if bf is None:
                output_blinding_factors[i] = Uint256()

        for i, bf in enumerate(output_asset_blinding_factors):
            if bf is None:
                output_asset_blinding_factors[i] = Uint256()

        return BlindingSuccess(
            num_successfully_blinded=nSuccessfullyBlinded,
            blinding_factors=output_blinding_factors,
            asset_blinding_factors=output_asset_blinding_factors)

    # Each input might contain up to two issuance pseudo-inputs,
    # thus there might be upt to 3 surjection trargets per input
    num_targets = len(tx.vin)*3

    if auxiliary_generators:
        assert len(auxiliary_generators) >= len(tx.vin)
        # We might have allow to set None for unused assetcommitments,
        # but the Elements Core API requires all assetcommitments to be
        # specified ad 33-byte chunks.
        # We do the same, to be close to the originial.
        assert all(isinstance(ag, (bytes, bytearray))
                   for ag in auxiliary_generators)
        assert all(len(ag) == 33 for ag in auxiliary_generators)

        # auxiliary_generators beyond the length of the input array
        # will also be used to fill surjectionTargets and targetAssetGenerators
        num_targets += len(auxiliary_generators)-len(tx.vin)

    output_blinding_factors = [None for _ in range(len(tx.vout))]
    output_asset_blinding_factors = [None for _ in range(len(tx.vout))]

    nBlindAttempts = 0
    nIssuanceBlindAttempts = 0
    nSuccessfullyBlinded = 0

    # Surjection proof prep

    # Needed to surj init, only matches to output asset matters, rest can be garbage
    surjectionTargets = [None for _ in range(num_targets)]
    # Needed to construct the proof itself. Generators must match final transaction to be valid
    targetAssetGenerators = [None for _ in range(num_targets)]

    # input_asset_blinding_factors is only for inputs, not for issuances(0 by def)
    # but we need to create surjection proofs against this list so we copy and insert 0's
    # where issuances occur.
    targetAssetBlinders = []

    totalTargets = 0

    for i in range(len(tx.vin)):
        # For each input we either need the asset/blinds or the generator
        if input_descriptors[i].asset.is_null():
            # If non-empty generator exists, parse
            if auxiliary_generators:
                # Parse generator here
                asset_generator = ctypes.create_string_buffer(SECP256K1_GENERATOR_SIZE)
                result = _secp256k1.secp256k1_generator_parse(
                    secp256k1_blind_context, asset_generator, auxiliary_generators[i])
                if result != 1:
                    assert result == 0
                    return BlindingFailure(
                        'auxiliary generator %d is not valid' % i)
            else:
                return BlindingFailure(
                    'input asset %d is empty, but auxiliary_generators '
                    'was not supplied' % i)
        else:
            asset_generator = ctypes.create_string_buffer(SECP256K1_GENERATOR_SIZE)
            ret = _secp256k1.secp256k1_generator_generate_blinded(
                secp256k1_blind_context, asset_generator,
                input_descriptors[i].asset.data,
                input_descriptors[i].asset_blinding_factor.data)
            assert ret == 1

        targetAssetGenerators[totalTargets] = asset_generator.raw
        surjectionTargets[totalTargets] = input_descriptors[i].asset
        targetAssetBlinders.append(input_descriptors[i].asset_blinding_factor)
        totalTargets += 1

        # Create target generators for issuances
        issuance = tx.vin[i].assetIssuance

        if not issuance.is_null():
            if issuance.nAmount.is_commitment():
                return BlindingFailure(
                    'issuance is not empty, but nAmount is a commitment')
            if issuance.nInflationKeys.is_commitment():
                return BlindingFailure(
                    'issuance is not empty, but nInflationKeys is a commitment')

            # New Issuance
            if issuance.assetBlindingNonce.is_null():
                blind_issuance = (len(blind_issuance_token_keys) > i
                                  and blind_issuance_token_keys[i] is not None)
                entropy = generate_asset_entropy(tx.vin[i].prevout, issuance.assetEntropy)
                asset = calculate_asset(entropy)
                token = calculate_reissuance_token(entropy, blind_issuance)
            else:
                asset = calculate_asset(issuance.assetEntropy)

            if not issuance.nAmount.is_null():
                surjectionTargets[totalTargets] = asset
                targetAssetGenerators[totalTargets] = ctypes.create_string_buffer(SECP256K1_GENERATOR_SIZE)
                ret = _secp256k1.secp256k1_generator_generate(
                    secp256k1_blind_context,
                    targetAssetGenerators[totalTargets], asset.data)
                assert ret == 1
                # Issuance asset cannot be blinded by definition
                targetAssetBlinders.append(Uint256())
                totalTargets += 1

            if not issuance.nInflationKeys.is_null():
                assert not token.is_null()
                surjectionTargets[totalTargets] = token
                targetAssetGenerators[totalTargets] = ctypes.create_string_buffer(SECP256K1_GENERATOR_SIZE)
                ret = _secp256k1.secp256k1_generator_generate(
                    secp256k1_blind_context,
                    targetAssetGenerators[totalTargets], token.data)
                assert ret == 1
                # Issuance asset cannot be blinded by definition
                targetAssetBlinders.append(Uint256())
                totalTargets += 1

    if auxiliary_generators:
        # Process any additional targets from auxiliary_generators
        # we know nothing about it other than the generator itself
        for n, ag in enumerate(auxiliary_generators[len(tx.vin):]):
            gen_buf = ctypes.create_string_buffer(SECP256K1_GENERATOR_SIZE)
            ret = _secp256k1.secp256k1_generator_parse(
                secp256k1_blind_context,
                gen_buf, auxiliary_generators[len(tx.vin)+n])
            if ret != 1:
                assert ret == 0
                return BlindingFailure(
                    'auxiliary generator %d is not valid' % len(tx.vin)+n)

            targetAssetGenerators[totalTargets] = gen_buf.raw
            surjectionTargets[totalTargets] = Uint256()
            targetAssetBlinders.append(Uint256())
            totalTargets += 1

    # Resize the target surjection lists to how many actually exist
    assert totalTargets == len(targetAssetBlinders)
    assert all(elt is None for elt in surjectionTargets[totalTargets:])
    assert all(elt is None for elt in targetAssetGenerators[totalTargets:])
    surjectionTargets = surjectionTargets[:totalTargets]
    targetAssetGenerators = targetAssetGenerators[:totalTargets]

    # Total blinded inputs that you own (that you are balancing against)
    nBlindsIn = 0
    # Number of outputs and issuances to blind
    nToBlind = 0

    blinds = []
    assetblinds = []
    amounts_to_blind = []

    for nIn in range(len(tx.vin)):
        if (
            not input_descriptors[nIn].blinding_factor.is_null()
            or not input_descriptors[nIn].asset_blinding_factor.is_null()
        ):
            if input_descriptors[nIn].amount < 0:
                return BlindingFailure(
                    'input blinding factors (or asset blinding factors) '
                    'for input %d is not empty, but amount specified for '
                    'this input is negative' % nIn)
            blinds.append(input_descriptors[nIn].blinding_factor)
            assetblinds.append(input_descriptors[nIn].asset_blinding_factor)
            amounts_to_blind.append(input_descriptors[nIn].amount)
            nBlindsIn += 1

        # Count number of issuance pseudo-inputs to blind
        issuance = tx.vin[nIn].assetIssuance
        if not issuance.is_null():
            # Marked for blinding
            if len(blind_issuance_asset_keys) > nIn and blind_issuance_asset_keys[nIn] is not None:
                if not issuance.nAmount.is_explicit():
                    return BlindingFailure(
                        'blind_issuance_asset_keys is specified for input %d, ',
                        'but issuance.nAmount is not explicit' % nIn)

                if len(tx.wit.vtxinwit) <= nIn \
                        or len(tx.wit.vtxinwit[nIn].issuanceAmountRangeproof) == 0:
                    nToBlind += 1
                else:
                    return BlindingFailure(
                        'blind_issuance_asset_keys is specified for input %d, ',
                        'but issuanceAmountRangeproof is already in place')

            if len(blind_issuance_token_keys) > nIn and blind_issuance_token_keys[nIn] is not None:
                if not issuance.nInflationKeys.is_explicit():
                    return BlindingFailure(
                        'blind_issuance_token_keys is specified for input %d, ',
                        'but issuance.nInflationKeys is not explicit' % nIn)
                if len(tx.wit.vtxinwit) <= nIn \
                        or len(tx.wit.vtxinwit[nIn].inflationKeysRangeproof) == 0:
                    nToBlind += 1
                else:
                    return BlindingFailure(
                        'blind_issuance_token_keys is specified for input %d, ',
                        'but inflationKeysRangeproof is already in place')

    for nOut, out_pub in enumerate(output_pubkeys):
        if out_pub.is_valid():
            # Keys must be valid and outputs completely unblinded or else call fails
            if not out_pub.is_fullyvalid():
                return BlindingFailure(
                    'blinding pubkey for output %d is not valid' % nOut)
            if not tx.vout[nOut].nValue.is_explicit():
                return BlindingFailure(
                    'valid blinding pubkey specified for output %d, '
                    'but nValue for this output is not explicit' % nOut)
            if not tx.vout[nOut].nAsset.is_explicit():
                return BlindingFailure(
                    'valid blinding pubkey specified for output %d, '
                    'but nAsset for this output is not explicit' % nOut)
            if len(tx.wit.vtxoutwit) > nOut and not tx.wit.vtxoutwit[nOut].is_null():
                return BlindingFailure(
                    'valid blinding pubkey specified for output %d, '
                    'but txout witness for this output is already in place' % nOut)
            if tx.vout[nOut].is_fee():
                return BlindingFailure(
                    'valid blinding pubkey specified for output %d, '
                    'but this output is a fee output' % nOut)

            nToBlind += 1

    # First blind issuance pseudo-inputs
    for nIn, txin in enumerate(tx.vin):
        asset_issuace_valid = (len(blind_issuance_asset_keys) > nIn
                               and blind_issuance_asset_keys[nIn] is not None)
        token_issuance_valid = (len(blind_issuance_token_keys) > nIn and
                                blind_issuance_token_keys[nIn] is not None)
        for nPseudo in range(2):
            if nPseudo == 0:
                iss_valid = asset_issuace_valid
            else:
                iss_valid = token_issuance_valid

            if iss_valid:
                nBlindAttempts += 1

                nIssuanceBlindAttempts += 1

                issuance = tx.vin[nIn].assetIssuance

                # First iteration does issuance asset, second inflation keys
                explicitValue = issuance.nInflationKeys if nPseudo else issuance.nAmount
                if explicitValue.is_null():
                    continue

                amount = explicitValue.to_amount()

                amounts_to_blind.append(amount)

                # Derive the asset of the issuance asset/token
                if issuance.assetBlindingNonce.is_null():
                    entropy = generate_asset_entropy(tx.vin[nIn].prevout, issuance.assetEntropy)
                    if nPseudo == 0:
                        asset = calculate_asset(entropy)
                    else:
                        assert token_issuance_valid
                        asset = calculate_reissuance_token(entropy, token_issuance_valid)
                else:
                    if nPseudo == 0:
                        asset = calculate_asset(issuance.assetEntropy)
                    else:
                        # Re-issuance only has one pseudo-input maximum
                        continue

                # Fill out the value blinders and blank asset blinder
                blinds.append(Uint256(_rand_func(32)))
                # Issuances are not asset-blinded
                assetblinds.append(Uint256())

                if nBlindAttempts == nToBlind:
                    # All outputs we own are unblinded, we don't support this type of blinding
                    # though it is possible. No privacy gained here, incompatible with secp api
                    return blinding_success(nSuccessfullyBlinded)

                while len(tx.wit.vtxinwit) <= nIn:
                    tx.wit.vtxinwit.append(CElementsMutableTxInWitness())

                txinwit = tx.wit.vtxinwit[nIn]

                # TODO Store the blinding factors of issuance

                # Create unblinded generator.
                (_, gen) = blinded_asset(asset, assetblinds[-1])

                # Create value commitment
                (confValue, commit) = create_value_commitment(blinds[-1].data, gen, amount)

                if nPseudo:
                    issuance = CAssetIssuance(
                        assetBlindingNonce=issuance.assetBlindingNonce,
                        assetEntropy=issuance.assetEntropy,
                        nAmount=issuance.nAmount,
                        nInflationKeys=confValue)
                else:
                    issuance = CAssetIssuance(
                        assetBlindingNonce=issuance.assetBlindingNonce,
                        assetEntropy=issuance.assetEntropy,
                        nAmount=confValue,
                        nInflationKeys=issuance.nInflationKeys)

                tx.vin[nIn].assetIssuance = issuance

                # nonce should just be blinding key
                if nPseudo == 0:
                    nonce = Uint256(blind_issuance_asset_keys[nIn].secret_bytes)
                else:
                    nonce = Uint256(blind_issuance_token_keys[nIn].secret_bytes)

                # Generate rangeproof, no script committed for issuances
                rangeproof = generate_rangeproof(
                    blinds, nonce, amount, CElementsScript(), commit, gen, asset, assetblinds)

                if nPseudo == 0:
                    txinwit.issuanceAmountRangeproof = rangeproof
                else:
                    txinwit.inflationKeysRangeproof = rangeproof

                # Successfully blinded this issuance
                nSuccessfullyBlinded += 1

    # This section of code *only* deals with unblinded outputs
    # that we want to blind
    for nOut, out_pub in enumerate(output_pubkeys):
        if out_pub.is_fullyvalid():
            out = tx.vout[nOut]
            nBlindAttempts += 1
            explicitValue = out.nValue
            amount = explicitValue.to_amount()
            asset = out.nAsset.to_asset()
            amounts_to_blind.append(amount)

            blinds.append(Uint256(_rand_func(32)))
            assetblinds.append(Uint256(_rand_func(32)))

            # Last blinding factor r' is set as -(output's (vr + r') - input's (vr + r')).
            # Before modifying the transaction or return arguments we must
            # ensure the final blinding factor to not be its corresponding -vr (aka unblinded),
            # or 0, in the case of 0-value output, insisting on additional output to blind.
            if nBlindAttempts == nToBlind:

                # Can't successfully blind in this case, since -vr = r
                # This check is assuming blinds are generated randomly
                # Adversary would need to create all input blinds
                # therefore would already know all your summed output amount anyways.
                if nBlindAttempts == 1 and nBlindsIn == 0:
                    return blinding_success(nSuccessfullyBlinded)

                blindedAmounts = (ctypes.c_uint64 * len(amounts_to_blind))(*amounts_to_blind)
                assetblindptrs = (ctypes.c_char_p*len(assetblinds))()
                for i, ab in enumerate(assetblinds):
                    assetblindptrs[i] = ab.data

                # Last blind will be written to
                # by secp256k1_pedersen_blind_generator_blind_sum(),
                # so we need to convert it into mutable buffer beforehand
                last_blind = ctypes.create_string_buffer(blinds[-1].data, len(blinds[-1].data))
                blindptrs = (ctypes.c_char_p*len(blinds))()
                for i, blind in enumerate(blinds[:-1]):
                    blindptrs[i] = blind.data

                blindptrs[-1] = ctypes.cast(last_blind, ctypes.c_char_p)

                # Check that number of blinds match. This is important
                # because this number is used by
                # secp256k1_pedersen_blind_generator_blind_sum() to get the
                # index of last blind, and that blinding factor will be overwritten.
                assert len(blindptrs) == nBlindAttempts + nBlindsIn

                assert(len(amounts_to_blind) == len(blindptrs))

                _immutable_check_hash = hashlib.sha256(b''.join(b.data for b in blinds)).digest()

                # Generate value we intend to insert
                ret = _secp256k1.secp256k1_pedersen_blind_generator_blind_sum(
                    secp256k1_blind_context,
                    blindedAmounts, assetblindptrs, blindptrs,
                    nBlindAttempts + nBlindsIn, nIssuanceBlindAttempts + nBlindsIn)

                assert ret == 1

                assert(_immutable_check_hash == hashlib.sha256(b''.join(b.data
                                                                        for b in blinds)).digest()),\
                    ("secp256k1_pedersen_blind_generator_blind_sum should not change "
                        "blinding factors other than the last one. Failing this assert "
                        "probably means that we supplied incorrect parameters to the function.")

                blinds[-1] = Uint256(bytes(last_blind))

                # Resulting blinding factor can sometimes be 0
                # where inputs are the negations of each other
                # and the unblinded value of the output is 0.
                # e.g. 1 unblinded input to 2 blinded outputs,
                # then spent to 1 unblinded output. (vr + r')
                # becomes just (r'), if this is 0, we can just
                # abort and not blind and the math adds up.
                # Count as success(to signal caller that nothing wrong) and return early
                if blinds[-1].is_null():
                    nSuccessfullyBlinded += 1
                    return blinding_success(nSuccessfullyBlinded)

            while len(tx.wit.vtxoutwit) <= nOut:
                tx.wit.vtxoutwit.append(CElementsMutableTxOutWitness())

            txoutwit = tx.wit.vtxoutwit[nOut]

            output_blinding_factors[nOut] = blinds[-1]
            output_asset_blinding_factors[nOut] = assetblinds[-1]

            # Blind the asset ID
            (confAsset, gen) = blinded_asset(asset, assetblinds[-1])

            out.nAsset = confAsset

            # Create value commitment
            (confValue, commit) = create_value_commitment(blinds[-1].data, gen, amount)

            out.nValue = confValue

            # Generate nonce for rewind by owner
            (nonce, ephemeral_pubkey) = generate_output_rangeproof_nonce(
                output_pubkeys[nOut], _rand_func=_rand_func)
            out.nNonce = CConfidentialNonce(bytes(ephemeral_pubkey))

            # Generate rangeproof
            txoutwit.rangeproof = generate_rangeproof(
                blinds, nonce, amount, out.scriptPubKey, commit, gen, asset, assetblinds)

            # Create surjection proof for this output
            surjectionproof = generate_surjectionproof(
                surjectionTargets, targetAssetGenerators,
                targetAssetBlinders, assetblinds, gen, asset,
                _rand_func=_rand_func)

            if not surjectionproof:
                continue

            txoutwit.surjectionproof = surjectionproof

            # Successfully blinded this output
            nSuccessfullyBlinded += 1

    return blinding_success(nSuccessfullyBlinded)


def generate_asset_entropy(prevout, contracthash):
    assert isinstance(prevout, COutPoint)
    assert isinstance(contracthash, (bytes, bytearray, Uint256))
    if isinstance(contracthash, Uint256):
        contracthash = contracthash.data
    assert len(contracthash) == 32
    return Uint256(CSHA256().Write(prevout.GetHash()).Write(contracthash).Midstate())


def calculate_asset(entropy):
    assert isinstance(entropy, (bytes, bytearray, Uint256))
    if isinstance(entropy, Uint256):
        entropy = entropy.data
    assert len(entropy) == 32
    return CAsset(CSHA256().Write(entropy).Write(Uint256().data).Midstate())


def calculate_reissuance_token(entropy, is_confidential):
    assert isinstance(entropy, (bytes, bytearray, Uint256))
    if isinstance(entropy, Uint256):
        entropy = entropy.data
    assert len(entropy) == 32
    if is_confidential:
        k = Uint256.from_int(2)
    else:
        k = Uint256.from_int(1)
    return CAsset(CSHA256().Write(entropy).Write(k.data).Midstate())


def blinded_asset(asset, assetblind):
    assert isinstance(asset, CAsset)
    assert isinstance(assetblind, Uint256)

    gen = ctypes.create_string_buffer(SECP256K1_GENERATOR_SIZE)
    ret = _secp256k1.secp256k1_generator_generate_blinded(
        secp256k1_blind_context, gen, asset.data, assetblind.data)
    assert ret == 1
    result_commitment = ctypes.create_string_buffer(CConfidentialAsset._committedSize)
    ret = _secp256k1.secp256k1_generator_serialize(
        secp256k1_blind_context, result_commitment, gen)
    assert ret == 1

    confAsset = CConfidentialAsset(bytes(result_commitment))
    assert confAsset.is_valid()

    return (confAsset, bytes(gen))


def create_value_commitment(blind, gen, amount):
    commit = ctypes.create_string_buffer(SECP256K1_PEDERSEN_COMMITMENT_SIZE)
    ret = _secp256k1.secp256k1_pedersen_commit(
        secp256k1_blind_context, commit, blind, amount, gen)
    assert ret == 1
    result_commitment = ctypes.create_string_buffer(CConfidentialAsset._committedSize)
    ret = _secp256k1.secp256k1_pedersen_commitment_serialize(
        secp256k1_blind_context, result_commitment, commit)
    assert ret == 1

    confValue = CConfidentialValue(bytes(result_commitment))
    assert confValue.is_valid()

    return (confValue, bytes(commit))


def generate_rangeproof(in_blinds, nonce, amount, scriptPubKey, commit, gen, asset, in_assetblinds):
    # NOTE: This is better done with typing module,
    # available since python3.5. but that means we will have
    # to add a dependency for python 3.4.
    # when we drop python3.4 support, we might use typing.
    assert isinstance(nonce, Uint256)
    assert isinstance(amount, int)
    assert isinstance(scriptPubKey, CElementsScript)
    assert isinstance(commit, (bytes, bytearray))
    assert len(commit) == SECP256K1_PEDERSEN_COMMITMENT_SIZE
    assert isinstance(asset, CAsset)
    assert isinstance(gen, (bytes, bytearray))
    assert len(gen) == SECP256K1_GENERATOR_SIZE

    # Note: the code only uses the single last elements of blinds and
    # assetblinds. We could require these elements to be passed explicitly,
    # but we will try to be close to original code.
    blind = in_blinds[-1]
    assert isinstance(blind, Uint256)
    assetblind = in_assetblinds[-1]
    assert isinstance(assetblind, Uint256)

    # Prep range proof
    nRangeProofLen = ctypes.c_size_t(5134)

    # TODO: smarter min_value selection

    rangeproof = ctypes.create_string_buffer(nRangeProofLen.value)

    # Compose sidechannel message to convey asset info (ID and asset blinds)
    assetsMessage = asset.data + assetblind.data

    chain_params = get_current_chain_params()

    try:
        ct_exponent = min(max(chain_params.CT_EXPONENT, -1), 18)
        ct_bits = min(max(chain_params.CT_BITS, 1), 51)
    except AttributeError:
        raise ValueError(
            'current chain params must define CT_EXPONENT and CT_BITS')

    # Sign rangeproof
    # If min_value is 0, scriptPubKey must be unspendable
    res = _secp256k1.secp256k1_rangeproof_sign(
        secp256k1_blind_context,
        rangeproof, ctypes.byref(nRangeProofLen),
        0 if scriptPubKey.is_unspendable() else 1,
        commit, blind.data, nonce.data, ct_exponent, ct_bits,
        amount, assetsMessage, len(assetsMessage),
        None if len(scriptPubKey) == 0 else scriptPubKey,
        len(scriptPubKey),
        gen)

    assert res == 1

    return rangeproof[:nRangeProofLen.value]


# Creates ECDH nonce commitment using ephemeral key and output_pubkey
def generate_output_rangeproof_nonce(output_pubkey, _rand_func=os.urandom):
    # Generate ephemeral key for ECDH nonce generation
    ephemeral_key = CKey.from_secret_bytes(_rand_func(32))
    ephemeral_pubkey = ephemeral_key.pub
    assert len(ephemeral_pubkey) == CConfidentialNonce._committedSize
    # Generate nonce
    nonce = ephemeral_key.ECDH(output_pubkey)
    nonce = Uint256(hashlib.sha256(nonce).digest())
    return nonce, ephemeral_pubkey


# Create surjection proof
def generate_surjectionproof(surjectionTargets, targetAssetGenerators,
                             targetAssetBlinders, assetblinds, gen, asset,
                             _rand_func=os.urandom):

    # Note: the code only uses the single last elements of assetblinds.
    # We could require these elements to be passed explicitly,
    # but we will try to be close to original code.

    nInputsToSelect = min(3, len(surjectionTargets))
    randseed = _rand_func(32)

    input_index = ctypes.c_size_t()
    proof = ctypes.c_void_p()

    ret = _secp256k1.secp256k1_surjectionproof_allocate_initialized(
        secp256k1_blind_context,
        ctypes.byref(proof), ctypes.byref(input_index),
        build_aligned_data_array([st.data for st in surjectionTargets], 32),
        len(surjectionTargets),
        nInputsToSelect, asset.data, 100, randseed)

    if ret == 0:
        # probably asset did not match any surjectionTargets
        return None

    try:
        ephemeral_input_tags_buf = build_aligned_data_array(targetAssetGenerators, 64)

        ret = _secp256k1.secp256k1_surjectionproof_generate(
            secp256k1_blind_context, proof,
            ephemeral_input_tags_buf, len(targetAssetGenerators),
            gen, input_index, targetAssetBlinders[input_index.value].data, assetblinds[-1].data)

        assert ret == 1

        ret = _secp256k1.secp256k1_surjectionproof_verify(
            secp256k1_blind_context, proof,
            ephemeral_input_tags_buf, len(targetAssetGenerators), gen)

        assert ret == 1

        expected_output_len = _secp256k1.secp256k1_surjectionproof_serialized_size(
            secp256k1_blind_context, proof)
        output_len = ctypes.c_size_t(expected_output_len)
        serialized_proof = ctypes.create_string_buffer(output_len.value)
        _secp256k1.secp256k1_surjectionproof_serialize(
            secp256k1_blind_context, serialized_proof, ctypes.byref(output_len), proof)
        assert output_len.value == expected_output_len
    finally:
        _secp256k1.secp256k1_surjectionproof_destroy(proof)

    return serialized_proof.raw


def unblind_confidential_pair(key, confValue, confAsset, nNonce,  # noqa
                              committedScript, rangeproof):
    """Unblinds a pair of confidential value and confidential asset
    given key, nonce, committed script, and rangeproof.
    returns a tuple of (success, result)
    If success is True, result is BlindingInputDescriptor namedtuple.
    If success is False, result is a string describing the cause of failure"""
    assert isinstance(key, CKeyMixin)
    assert isinstance(confValue, CConfidentialValue)
    assert isinstance(confAsset, CConfidentialAsset)
    assert isinstance(nNonce, CConfidentialNonce)
    assert isinstance(committedScript, CElementsScript)
    assert isinstance(rangeproof, (bytes, bytearray))

    # NOTE: we do not allow creation of invalid CKey instances,
    # so no key.is_valid() check needed

    if len(rangeproof) == 0:
        return UnblindingFailure('rangeproof is empty')

    ephemeral_key = CPubKey(nNonce.commitment)

    # ECDH or not depending on if nonce commitment is non-empty
    if len(nNonce.commitment) > 0:
        if not ephemeral_key.is_fullyvalid():
            return UnblindingFailure('nNonce.commitment is not a valid pubkey')
        nonce = hashlib.sha256(key.ECDH(ephemeral_key)).digest()
    else:
        # Use blinding key directly, and don't commit to a scriptpubkey
        committedScript = CElementsScript()
        nonce = key.secret_bytes

    # 32 bytes of asset type, 32 bytes of asset blinding factor in sidechannel
    msg_size = ctypes.c_size_t(64)
    # API-prescribed sidechannel maximum size,
    # though we only use 64 bytes
    msg = ctypes.create_string_buffer(4096)

    # If value is unblinded, we don't support unblinding just the asset
    if not confValue.is_commitment():
        return UnblindingFailure('value is not a commitment')

    observed_gen = ctypes.create_string_buffer(64)
    # Valid asset commitment?
    if confAsset.is_commitment():
        res = _secp256k1.secp256k1_generator_parse(
            secp256k1_blind_context, observed_gen, confAsset.commitment)
        if res != 1:
            assert res == 0
            return UnblindingFailure(
                'cannot parse asset commitment as a generator')
    elif confAsset.is_explicit():
        res = _secp256k1.secp256k1_generator_generate(
            secp256k1_blind_context, observed_gen, confAsset.to_asset().data)
        if res != 1:
            assert res == 0
            return UnblindingFailure(
                'unable to create a generator out of asset explicit data')

    commit = ctypes.create_string_buffer(64)
    # Valid value commitment ?
    res = _secp256k1.secp256k1_pedersen_commitment_parse(secp256k1_blind_context,
                                                         commit, confValue.commitment)
    if res != 1:
        assert res == 0
        return UnblindingFailure(
            'cannot parse value commitment as Pedersen commitment')

    blinding_factor_out = ctypes.create_string_buffer(32)

    min_value = ctypes.c_uint64()
    max_value = ctypes.c_uint64()
    amount = ctypes.c_uint64()

    res = _secp256k1.secp256k1_rangeproof_rewind(
        secp256k1_blind_context,
        blinding_factor_out,
        ctypes.byref(amount),
        msg, ctypes.byref(msg_size),
        nonce,
        ctypes.byref(min_value), ctypes.byref(max_value),
        commit, rangeproof, len(rangeproof),
        committedScript or None, len(committedScript),
        observed_gen)

    if 0 == res:
        return UnblindingFailure('unable to rewind rangeproof')

    assert res == 1

    if not MoneyRange(amount.value):
        return UnblindingFailure(
            'resulting amount after rangeproof rewind is outside MoneyRange')

    if msg_size.value != 64:
        return UnblindingFailure(
            'resulting message after rangeproof rewind is not 64 bytes in size')

    asset_type = msg
    asset_blinder = msg[32:]
    recalculated_gen = ctypes.create_string_buffer(64)
    res = _secp256k1.secp256k1_generator_generate_blinded(
        secp256k1_blind_context, recalculated_gen, asset_type, asset_blinder)
    if res != 1:
        assert res == 0
        return UnblindingFailure(
            'unable to recalculate a generator from asset type and asset blinder '
            'resulted from rangeproof rewind')

    # Serialize both generators then compare

    observed_generator = ctypes.create_string_buffer(33)
    derived_generator = ctypes.create_string_buffer(33)
    res = _secp256k1.secp256k1_generator_serialize(
        secp256k1_blind_context, observed_generator, observed_gen)
    assert res == 1

    res = _secp256k1.secp256k1_generator_serialize(
        secp256k1_blind_context, derived_generator, recalculated_gen)
    assert res == 1

    if observed_generator.raw != derived_generator.raw:
        return UnblindingFailure(
            'generator recalculated after rangeproof rewind '
            'does not match generator presented in asset commitment')

    return UnblindingSuccess(
        amount=amount.value, blinding_factor=Uint256(blinding_factor_out.raw),
        asset=CAsset(asset_type[:32]), asset_blinding_factor=Uint256(msg[32:64]))


def derive_blinding_key(blinding_derivation_key, script):
    assert isinstance(blinding_derivation_key, CKeyMixin)
    # based on Elements Core's blinding key derivation logic
    # as of commit 43f6cdbd3147d9af450b73c8b8b8936e3e4166df
    return CKey(hmac.new(blinding_derivation_key.secret_bytes, script,
                         hashlib.sha256).digest())


ZKPRangeproofInfo = namedtuple('ZKPRangeproofInfo',
                               'exp mantissa value_min value_max')

BlindingInputDescriptor = namedtuple('BlindingInputDescriptor',
                                     ('asset',
                                      'amount',
                                      'blinding_factor',
                                      'asset_blinding_factor'))


class BlindingOrUnblindingResult(metaclass=abc.ABCMeta):
    def __bool__(self):
        raise TypeError(
            'Using {} as boolean is not corect usage. '
            'please use {}.error or {}.ok'.format(
                self.__class__.__name__, self.__class__.__name__,
                self.__class__.__name__))

    @abc.abstractmethod
    def error(self):
        ...

    def ok(self):
        err = self.error()

        if err is None:
            return True

        assert isinstance(err, str)
        assert bool(err)

        return False


class BlindingOrUnblindingFailure(BlindingOrUnblindingResult):
    @property
    def error(self):
        return self._error_message

    def __init__(self, error_msg):
        assert isinstance(error_msg, str)
        assert bool(error_msg), "error string must not be empty"
        self._error_message = error_msg

    def __str__(self):
        return '{}("{}")'.format(self.__class__.__name__, self.error)


class BlindingOrUnblindingSuccess(BlindingOrUnblindingResult):
    @property
    def error(self):
        return None


class BlindingFailure(BlindingOrUnblindingFailure):
    ...


class BlindingSuccess(BlindingOrUnblindingSuccess,
                      namedtuple('BlindingSuccess',
                                 ('num_successfully_blinded',
                                  'blinding_factors',
                                  'asset_blinding_factors'))):
    __slots__ = ()

    def __init__(self, *args, **kwargs):
        # For newer python versions, type annotations can be used to
        # enforce correct types.
        # currently we target python3.4 - maybe if we drop support for older
        # python versions, this could be rewritten to use type annotations.
        assert isinstance(self.num_successfully_blinded, int)
        assert all(isinstance(bf, Uint256) for bf in self.blinding_factors)
        assert all(isinstance(bf, Uint256) for bf in self.asset_blinding_factors)
        super(BlindingSuccess, self).__init__()


class UnblindingFailure(BlindingOrUnblindingFailure):
    ...


class UnblindingSuccess(BlindingOrUnblindingSuccess,
                        namedtuple('UnblindingSuccess',
                                   ('asset',
                                    'amount',
                                    'blinding_factor',
                                    'asset_blinding_factor'))):
    def __init__(self, *args, **kwargs):
        assert isinstance(self.amount, int)
        assert MoneyRange(self.amount)
        assert isinstance(self.asset, CAsset)
        assert isinstance(self.blinding_factor, Uint256)
        assert isinstance(self.asset_blinding_factor, Uint256)
        super(UnblindingSuccess, self).__init__()

    def get_descriptor(self):
        return BlindingInputDescriptor(
            asset=self.asset, amount=self.amount,
            blinding_factor=self.blinding_factor,
            asset_blinding_factor=self.asset_blinding_factor)


ElementsTransactionIdentityMeta.set_classmap({
    CTransaction: CElementsTransaction,
    CTxIn: CElementsTxIn,
    CTxOut: CElementsTxOut,
    CTxWitness: CElementsTxWitness,
    CTxInWitness: CElementsTxInWitness,
    CTxOutWitness: CElementsTxOutWitness,
    COutPoint: CElementsOutPoint,
})

ElementsMutableTransactionIdentityMeta.set_classmap({
    CMutableTransaction: CElementsMutableTransaction,
    CMutableTxIn: CElementsMutableTxIn,
    CMutableTxOut: CElementsMutableTxOut,
    CMutableTxWitness: CElementsMutableTxWitness,
    CMutableTxInWitness: CElementsMutableTxInWitness,
    CMutableTxOutWitness: CElementsMutableTxOutWitness,
    CMutableOutPoint: CElementsMutableOutPoint,
})

ElementsMutableTransactionIdentityMeta.set_mutable_immutable_links(
    ElementsTransactionIdentityMeta)

__all__ = (
    'CAsset',
    'CAssetIssuance',
    'CConfidentialAsset',
    'CConfidentialValue',
    'CConfidentialNonce',
    'derive_blinding_key',
    'generate_asset_entropy',
    'calculate_asset',
    'calculate_reissuance_token',
    'BlindingInputDescriptor',
    'BlindingSuccess',
    'BlindingFailure',
    'UnblindingSuccess',
    'UnblindingFailure',
    'CElementsOutPoint',
    'CElementsMutableOutPoint',
    'CElementsTxIn',
    'CElementsMutableTxIn',
    'CElementsTxOut',
    'CElementsMutableTxOut',
    'CElementsTransaction',
    'CElementsMutableTransaction',
    'CElementsTxWitness',
    'CElementsMutableTxWitness',
    'CElementsMutableTxInWitness',
    'CElementsTxInWitness',
)