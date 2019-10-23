#!/usr/bin/env python3

from testUtils import Utils
import testUtils
import time
from Cluster import Cluster
from Cluster import NamedAccounts
from core_symbol import CORE_SYMBOL
from WalletMgr import WalletMgr
from Node import BlockType
from Node import Node
from TestHelper import TestHelper
from TestHelper import AppArgs

import decimal
import json
import math
import re
import signal

###############################################################
# nodeos_high_transaction_test
# 
# This test sets up 1 producing node and 1 non-producing node.
#   the non-producing node will be sent many transfers.  When
#   it is complete it verifies that all of the transactions
#   made it into blocks.
#
###############################################################

Print=Utils.Print

from core_symbol import CORE_SYMBOL

appArgs=AppArgs()
extraArgs = appArgs.add(flag="--transaction-time-delta", type=int, help="How many seconds seconds behind an earlier sent transaction should be received after a later one", default=5)
extraArgs = appArgs.add(flag="--num-transactions", type=int, help="How many total transactions should be sent", default=10000)
extraArgs = appArgs.add(flag="--max-transactions-per-second", type=int, help="How many transactions per second should be sent", default=500)
args = TestHelper.parse_args({"-p", "-n","--dump-error-details","--keep-logs","-v","--leave-running","--clean-run"}, applicationSpecificArgs=appArgs)

Utils.Debug=args.v
totalProducerNodes=args.p
totalNodes=args.n
if totalNodes<=totalProducerNodes:
    totalNodes=totalProducerNodes+1
totalNonProducerNodes=totalNodes-totalProducerNodes
maxActiveProducers=totalProducerNodes
totalProducers=totalProducerNodes
cluster=Cluster(walletd=True)
dumpErrorDetails=args.dump_error_details
keepLogs=args.keep_logs
dontKill=args.leave_running
killAll=args.clean_run
walletPort=TestHelper.DEFAULT_WALLET_PORT
blocksPerSec=2
transBlocksBehind=args.transaction_time_delta * blocksPerSec
numTransactions = args.num_transactions
maxTransactionsPerSecond = args.max_transactions_per_second

walletMgr=WalletMgr(True, port=walletPort)
testSuccessful=False
killEosInstances=not dontKill
killWallet=not dontKill

WalletdName=Utils.EosWalletName
ClientName="cleos"

try:
    TestHelper.printSystemInfo("BEGIN")

    cluster.setWalletMgr(walletMgr)
    cluster.killall(allInstances=killAll)
    cluster.cleanup()
    Print("Stand up cluster")

    if cluster.launch(pnodes=totalProducerNodes,
                      totalNodes=totalNodes, totalProducers=totalProducers,
                      useBiosBootFile=False) is False:
        Utils.cmdError("launcher")
        Utils.errorExit("Failed to stand up eos cluster.")

    # ***   create accounts to vote in desired producers   ***

    totalAccounts = 100
    Print("creating %d accounts" % (totalAccounts))
    namedAccounts=NamedAccounts(cluster,totalAccounts)
    accounts=namedAccounts.accounts

    accountsToCreate = [cluster.eosioAccount]
    for account in accounts:
        accountsToCreate.append(account)

    testWalletName="test"

    Print("Creating wallet \"%s\"." % (testWalletName))
    testWallet=walletMgr.create(testWalletName, accountsToCreate)

    for _, account in cluster.defProducerAccounts.items():
        walletMgr.importKey(account, testWallet, ignoreDupKeyWarning=True)

    Print("Wallet \"%s\" password=%s." % (testWalletName, testWallet.password.encode("utf-8")))

    for account in accounts:
        walletMgr.importKey(account, testWallet)

    # ***   identify each node (producers and non-producing node)   ***

    nonProdNodes=[]
    prodNodes=[]
    for i in range(0, totalNodes):
        node=cluster.getNode(i)
        nodeProducers=Cluster.parseProducers(i)
        numProducers=len(nodeProducers)
        Print("node has producers=%s" % (nodeProducers))
        if numProducers==0:
            nonProdNodes.append(node)
        else:
            prodNodes.append(node)
    nonProdNodeCount = len(nonProdNodes)

    # ***   delegate bandwidth to accounts   ***

    node=nonProdNodes[0]
    checkTransIds = []
    startTime = time.perf_counter()
    Print("Create new accounts via %s" % (cluster.eosioAccount.name))
    # create accounts via eosio as otherwise a bid is needed
    for account in accounts:
        trans = node.createInitializeAccount(account, cluster.eosioAccount, stakedDeposit=0, waitForTransBlock=False, stakeNet=1000, stakeCPU=1000, buyRAM=1000, exitOnError=True)
        checkTransIds.append(Node.getTransId(trans))

    nextTime = time.perf_counter()
    Print("Create new accounts took %s sec" % (nextTime - startTime))
    startTime = nextTime

    Print("Transfer funds to new accounts via %s" % (cluster.eosioAccount.name))
    for account in accounts:
        transferAmount="1000.0000 {0}".format(CORE_SYMBOL)
        Print("Transfer funds %s from account %s to %s" % (transferAmount, cluster.eosioAccount.name, account.name))
        trans = node.transferFunds(cluster.eosioAccount, account, transferAmount, "test transfer", waitForTransBlock=False, reportStatus=False)
        checkTransIds.append(Node.getTransId(trans))

    nextTime = time.perf_counter()
    Print("Transfer funds took %s sec" % (nextTime - startTime))
    startTime = nextTime

    Print("Delegate Bandwidth to new accounts")
    for account in accounts:
        trans=node.delegatebw(account, 200.0000, 200.0000, waitForTransBlock=False, exitOnError=True, reportStatus=False)
        checkTransIds.append(Node.getTransId(trans))

    nextTime = time.perf_counter()
    Print("Delegate Bandwidth took %s sec" % (nextTime - startTime))
    startTime = nextTime

    def cacheTransIdInBlock(blockNum, transToBlock):
        block = node.getBlock(blockNum)
        if block is None:
            return None
        transactions = block["transactions"]
        for trans_receipt in transactions:
            btrans = trans_receipt["trx"]
            assert btrans is not None, Print("ERROR: could not retrieve \"trx\" from transaction_receipt: %s, from transId: %s that led to block: %s" % (json.dumps(trans_receipt, indent=2), transId, json.dumps(block, indent=2)))
            btransId = btrans["id"]
            assert btransId is not None, Print("ERROR: could not retrieve \"id\" from \"trx\": %s, from transId: %s that led to block: %s" % (json.dumps(btrans, indent=2), transId, json.dumps(block, indent=2)))
            transToBlock[btransId] = block
        return block

    def findTransInBlock(transId, transToBlock, node):
        if transId in transToBlock:
            return
        trans = node.getTransaction(transId, delayedRetry=False)
        assert trans is not None, Print("ERROR: could not find transaction for transId: %s" % (transId))
        blockNum = node.getTransBlockNum(trans)
        assert blockNum is not None, Print("ERROR: could not retrieve block num from transId: %s, trans: %s" % (transId, json.dumps(trans, indent=2)))
        block = cacheTransIdInBlock(blockNum, transToBlock)
        assert block is not None, Print("ERROR: could not retrieve block with block num: %d, from transId: %s, trans: %s" % (blockNum, transId, json.dumps(trans, indent=2)))

    transToBlock = {}
    for transId in checkTransIds:
        findTransInBlock(transId, transToBlock, node)

    nextTime = time.perf_counter()
    Print("Verifying transactions took %s sec" % (nextTime - startTime))
    startTransferTime = nextTime

    #verify nodes are in sync and advancing
    cluster.waitOnClusterSync(blockAdvancing=5)

    Print("Sending %d transfers" % (numTransactions))
    numRounds = int(numTransactions / totalAccounts)
    delayAfterRounds = int(maxTransactionsPerSecond / totalAccounts)
    history = []
    startTime = time.perf_counter()
    startRound = None
    for round in range(0, numRounds):
        # ensure we are not sending too fast
        startRound = time.perf_counter()
        timeDiff = startRound - startTime
        expectedTransactions = maxTransactionsPerSecond * timeDiff
        sentTransactions = round * totalAccounts
        if sentTransactions > expectedTransactions:
            excess = sentTransactions - expectedTransactions
            # round up to a second
            delayTime = int((excess + maxTransactionsPerSecond - 1) / maxTransactionsPerSecond)
            Utils.Print("Delay %d seconds to keep max transactions under %d per second" % (delayTime, maxTransactionsPerSecond))
            time.sleep(delayTime)

        transferAmount = Node.currencyIntToStr(round + 1, CORE_SYMBOL)
        Print("Sending round %d, transfer: %s" % (round, transferAmount))
        for accountIndex in range(0, totalAccounts):
            fromAccount = accounts[accountIndex]
            toAccountIndex = accountIndex + 1 if accountIndex + 1 < totalAccounts else 0
            toAccount = accounts[toAccountIndex]
            node = nonProdNodes[accountIndex % nonProdNodeCount]
            trans=node.transferFunds(fromAccount, toAccount, transferAmount, "transfer round %d" % (round), exitOnError=False, reportStatus=False, signWith=fromAccount.activePublicKey)
            if trans is None:
                # delay and see if transfer is accepted now
                Utils.Print("Transfer rejected, delay 1 second and see if it is then accepted")
                time.sleep(1)
                trans=node.transferFunds(fromAccount, toAccount, transferAmount, "transfer round %d" % (round), exitOnError=False, reportStatus=False, signWith=fromAccount.activePublicKey)

            assert trans is not None, Print("ERROR: failed round: %d, fromAccount: %s, toAccount: %s" % (round, accountIndex, toAccountIndex))
            # store off the transaction id, which we can use with the node.transCache
            history.append(Node.getTransId(trans))

    nextTime = time.perf_counter()
    Print("Sending transfers took %s sec" % (nextTime - startTransferTime))
    startTranferValidationTime = nextTime

    blocks = {}
    transToBlock = {}
    missingTransactions = []
    transBlockOrderWeird = []
    newestBlockNum = None
    newestBlockNumTransId = None
    newestBlockNumTransOrder = None
    lastBlockNum = None
    lastTransId = None
    transOrder = 0
    for transId in history:
        trans = node.getTransaction(transId, delayedRetry=False)
        blockNum = None
        if trans is None:
            missingTransactions.append({
                "newer_trans_id" : transId,
                "newer_trans_index" : transOrder,
                "newer_bnum" : blockNum,
                "last_trans_id" : lastTransId,
                "last_trans_index" : transOrder - 1,
                "last_bnum" : lastBlockNum,
            })
        block = None
        if transId not in transToBlock:
            blockNum = node.getTransBlockNum(trans)
            assert blockNum is not None, Print("ERROR: could not retrieve block num from transId: %s, trans: %s" % (transId, json.dumps(trans, indent=2)))
            if lastBlockNum is not None:
                if blockNum > lastBlockNum + transBlocksBehind or blockNum + transBlocksBehind < lastBlockNum:
                    transBlockOrderWeird.append({
                        "newer_trans_id" : transId,
                        "newer_trans_index" : transOrder,
                        "newer_bnum" : blockNum,
                        "last_trans_id" : lastTransId,
                        "last_trans_index" : transOrder - 1,
                        "last_bnum" : lastBlockNum
                    })
                    if newestBlockNum > lastBlockNum:
                        last = transBlockOrderWeird[-1]
                        last["older_trans_id"] = newestBlockNumTransId
                        last["older_trans_index"] = newestBlockNumTransOrder
                        last["older_bnum"] = newestBlockNum

            if newestBlockNum is None:
                newestBlockNum = blockNum
                newestBlockNumTransId = transId
                newestBlockNumTransOrder = transOrder
            elif blockNum > newestBlockNum:
                newestBlockNum = blockNum
                newestBlockNumTransId = transId
                newestBlockNumTransOrder = transOrder

            if not cacheTransIdInBlock(blockNum, transToBlock):
                missingTransactions.append({
                    "newer_trans_id" : transId,
                    "newer_trans_index" : transOrder,
                    "newer_bnum" : blockNum,
                    "last_trans_id" : lastTransId,
                    "last_trans_index" : transOrder - 1,
                    "last_bnum" : lastBlockNum,
                })
                if newestBlockNum > lastBlockNum:
                    transBlockOrderWeird[-1]["highest_block_seen"] = newestBlockNum
        else:
            blockNum = transToBlock[transId]["block_num"]

        lastTransId = transId
        transOrder += 1
        lastBlockNum = blockNum

    nextTime = time.perf_counter()
    Print("Validating transfers took %s sec" % (nextTime - startTranferValidationTime))

    delayedReportError = False
    if len(missingTransactions) > 0:
        verboseOutput = "Missing transaction information: [" if Utils.Debug else "Missing transaction ids: ["
        first = True
        for missingTrans in missingTransactions:
            if not first:
                verboseOutput += ", "
            verboseOutput += missingTrans if Utils.Debug else missingTrans["newer_trans_id"]
            first = False

        verboseOutput += "]"
        Utils.Print("ERROR: There are %d missing transactions.  %s" % (len(missingTransactions), verboseOutput))
        delayedReportError = True

    if len(transBlockOrderWeird) > 0:
        verboseOutput = "Delayed transaction information: [" if Utils.Debug else "Delayed transaction ids: ["
        first = True
        for trans in transBlockOrderWeird:
            if not first:
                verboseOutput += ", "
            if Utils.Debug:
                verboseOutput += json.dumps(trans, indent=2)
            else:
                verboseOutput += trans["newer_trans_id"]
            first = False

        verboseOutput += "]"
        Utils.Print("ERROR: There are %d transactions delayed more than %d seconds.  %s" % (len(transBlockOrderWeird), args.transaction_time_delta, verboseOutput))
        delayedReportError = True

    testSuccessful = not delayedReportError
finally:
    TestHelper.shutdown(cluster, walletMgr, testSuccessful=testSuccessful, killEosInstances=killEosInstances, killWallet=killWallet, keepLogs=keepLogs, cleanRun=killAll, dumpErrorDetails=dumpErrorDetails)
    if not testSuccessful:
        Print(Utils.FileDivider)
        Print("Compare Blocklog")
        cluster.compareBlockLogs()
        Print(Utils.FileDivider)
        Print("Print Blocklog")
        cluster.printBlockLog()
        Print(Utils.FileDivider)

errorCode = 0 if testSuccessful else 1
exit(errorCode)