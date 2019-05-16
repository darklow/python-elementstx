# Copyright (C) 2019 The python-elementstx developers
#
# This file is part of python-elementstx.
#
# It is subject to the license terms in the LICENSE file found in the top-level
# directory of this distribution.
#
# No part of python-elementstx, including this file, may be copied, modified,
# propagated, or distributed except according to the terms contained in the
# LICENSE file.

from .version import __version__

import elementstx.core
import elementstx.wallet

from bitcointx import BitcoinMainnetParams


class ElementsParams(BitcoinMainnetParams):
    NAME = 'elements'
    RPC_PORT = 7041
    TRANSACTION_IDENTITY = elementstx.core.ElementsTransactionIdentityMeta
    WALLET_IDENTITY = elementstx.wallet.ElementsWalletIdentityMeta

    CT_EXPONENT = 0
    CT_BITS = 32

    def get_datadir_extra_name(self):
        name_parts = self.NAME.split('/')
        if len(name_parts) == 1:
            # Data dir for Elements is 'elementsregtest'
            return name_parts[0] + 'regtest'
        return name_parts[1]


__all__ = (
    '__version__',
    'ElementsParams'
)