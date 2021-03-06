from exchanges.exchange import Exchange
import asyncio
import websockets
import json
import time
from random import randrange
from models.block import Type as BlockType
from models.block import OrderType
from models.block import Direction
from models.block import Trigger
from plugins.plugin_loader import PluginLoader


class Deribit(Exchange):
    def __init__(self, client_id, client_secret, test=False):
        super().__init__()
        self.client_id = client_id
        self.client_secret = client_secret
        self.test = test

        if self.test:
            self.url = "test.deribit.com"
        else:
            self.url = "www.deribit.com"

    def processAlert(self, alert):
        if not alert.blocks:
            self.logger.error("No blocks found for alert")

        url = "wss://" + self.url + "/ws/api/v2"

        async def call_api():
            async with websockets.connect(url) as websocket:
                # authenticate
                await websocket.send(self.getAuthenticationJson())
                while websocket.open:
                    response = await websocket.recv()
                    j = json.loads(response)
                    if self.isErrorResponse("authentication", j):
                        return

                    # look up the tick size based on symbol
                    await websocket.send(self.getInstrumentsJson(
                        self.currency))
                    response = await websocket.recv()
                    j = json.loads(response)
                    if self.isErrorResponse("instrument request", j):
                        return
                    precision = next((item for item in
                                      j.get("result")
                                      if item["instrument_name"] ==
                                      alert.symbol),
                                     None).get("tick_size")

                    # get the account information
                    await websocket.send(self.getAccountInfoJson(
                        self.currency))
                    response = await websocket.recv()
                    j = json.loads(response)
                    if self.isErrorResponse("account info request", j):
                        return
                    accountInfo = j.get("result")

                    for block in alert.blocks:
                        if block and block.type:
                            if block.wait:
                                self.logger.info(
                                    "Waiting %s seconds", block.wait)
                                time.sleep(int(block.wait))

                            self.logger.info("Executing Block: %s",
                                             block.type.value)

                            if block.type == BlockType.CANCEL_ORDER:
                                await self.cancelOrder(websocket,
                                                       alert,
                                                       block)
                            elif block.type == BlockType.CLOSE_POSITION:
                                await self.closePosition(websocket,
                                                         precision,
                                                         alert,
                                                         block)
                            elif block.type == BlockType.STANDARD_ORDER:
                                await self.trade(websocket,
                                                 precision,
                                                 accountInfo,
                                                 alert,
                                                 block)
                            elif block.type == BlockType.PLUGIN:
                                continueProcessingBlocks = PluginLoader.processPluginBlock(alert,
                                                                              block)
                                if not continueProcessingBlocks:
                                    self.logger.warning(
                                        "Plugin %s returned False, skipping remaining blocks for alert", block.plugin)
                                    return

                    # close the websocket
                    await websocket.close()

        asyncio.new_event_loop().run_until_complete(call_api())

    def getJsonMessage(self, method, params):
        return json.dumps({
            "jsonrpc": "2.0",
            "id": randrange(0, 8001, 2),
            "method": method,
            "params": params
        })

    def getAuthenticationJson(self):
        params = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        return self.getJsonMessage("public/auth", params)

    def getAccountInfoJson(self, currency):
        params = {
            "currency": currency,
            "extended": True
        }
        return self.getJsonMessage("private/get_account_summary", params)

    def getTickerJson(self, instrument):
        params = {
            "instrument_name": instrument
        }
        return self.getJsonMessage("public/ticker", params)

    def getInstrumentsJson(self, currency):
        params = {
            "currency": currency,
            "kind": "future",
            "expired": False
        }
        return self.getJsonMessage("public/get_instruments", params)

    def getOpenOrdersJson(self, instrument):
        params = {
            "instrument_name": instrument
        }
        return self.getJsonMessage("private/get_open_orders_by_instrument",
                                   params)

    def getCancelJson(self, order_id):
        params = {
            "order_id": order_id
        }
        self.logger.debug("Cancel Order Params: %s", params)
        return self.getJsonMessage("private/cancel", params)

    def getPositionJson(self, instrument):
        params = {
            "instrument_name": instrument
        }
        return self.getJsonMessage("private/get_position", params)

    def getClosePositionJson(self, precision, block, position):
        if not position:
            self.logger.debug("No position returned")
            return

        if (position.get("size") and
            ((block.direction == Direction.BUY and
              position.get("size") > 0) or
             (block.direction == Direction.SELL and
              position.get("size") < 0) or
                (not block.direction and abs(position.get("size")) > 0))):

            method = ("private/sell" if position.get("direction") == "buy"
                      else "private/buy")
            params = {
                "instrument_name": position.get("instrument_name"),
                "amount": abs(self.toPrecise(self.changeQuantity(
                    position.get("size"), block.quantity), 0))
            }

            if block.orderType == OrderType.MARKET:
                params["type"] = "market"

            elif block.orderType == OrderType.LIMIT:
                params["type"] = "limit"
                params["price"] = self.toPrecise(
                    self.changePrice(position.get("average_price"),
                                     block.limit_price),
                    precision)
                params["reduce_only"] = True

                if block.post_only:
                    params["post_only"] = True

            elif block.orderType == OrderType.STOP_MARKET:
                params["type"] = "stop_market"
                params["stop_price"] = self.toPrecise(
                    self.changePrice(position.get("average_price"),
                                     block.stop_price),
                    precision)
                params["reduce_only"] = True

                if block.trigger == Trigger.LAST or not block.trigger:
                    params["trigger"] = "last_price"
                elif block.trigger == Trigger.INDEX:
                    params["trigger"] = "index_price"
                elif block.trigger == Trigger.MARK:
                    params["trigger"] = "mark_price"

            elif block.orderType == OrderType.STOP_LIMIT:
                params["type"] = "stop_limit"
                params["price"] = self.toPrecise(
                    self.changePrice(position.get("average_price"),
                                     block.limit_price),
                    precision)
                params["stop_price"] = self.toPrecise(
                    self.changePrice(position.get("average_price"),
                                     block.stop_price),
                    precision)

                if block.post_only:
                    params["post_only"] = True

                if block.trigger == Trigger.LAST or not block.trigger:
                    params["trigger"] = "last_price"
                elif block.trigger == Trigger.INDEX:
                    params["trigger"] = "index_price"
                elif block.trigger == Trigger.MARK:
                    params["trigger"] = "mark_price"
            self.logger.debug("Close Position method: %s, Params: %s",
                              method, params)
            return self.getJsonMessage(method, params)
        else:
            self.logger.debug(("Not in any valid position to close. "
                               "Current position Size: %s & direction: %s"),
                              position.get("size"),
                              (block.direction.value if block.direction
                               else block.direction))

    def getTradeJson(self, precision, ticker, accountInfo, alert, block):
        method = ("private/buy" if block.direction == Direction.BUY
                  else "private/sell")
        params = {
            "instrument_name": alert.symbol
        }

        if block.orderType == OrderType.MARKET:
            params["type"] = "market"

        elif block.orderType == OrderType.LIMIT:
            params["type"] = "limit"
            first = (ticker.get("best_bid_price") if block.direction ==
                     Direction.BUY else ticker.get("best_ask_price"))
            params["price"] = self.toPrecise(
                self.changePrice(first, block.limit_price), precision)

            if block.post_only:
                params["post_only"] = True

            if block.reduce_only:
                params["reduce_only"] = True

        elif block.orderType == OrderType.STOP_MARKET:
            params["type"] = "stop_market"
            first = (ticker.get("best_bid_price") if block.direction ==
                     Direction.SELL else ticker.get("best_ask_price"))
            params["stop_price"] = self.toPrecise(
                self.changePrice(first, block.stop_price), precision)

            if block.trigger == Trigger.LAST or not block.trigger:
                params["trigger"] = "last_price"
            elif block.trigger == Trigger.INDEX:
                params["trigger"] = "index_price"
            elif block.trigger == Trigger.MARK:
                params["trigger"] = "mark_price"

        elif block.orderType == OrderType.STOP_LIMIT:
            params["type"] = "stop_limit"
            first = (ticker.get("best_bid_price") if block.direction ==
                     Direction.BUY else ticker.get("best_ask_price"))
            params["price"] = self.toPrecise(
                self.changePrice(first, block.limit_price), precision)
            params["stop_price"] = self.toPrecise(
                self.changePrice(first, block.stop_price), precision)

            if block.post_only:
                params["post_only"] = True

            if block.trigger == Trigger.LAST or not block.trigger:
                params["trigger"] = "last_price"
            elif block.trigger == Trigger.INDEX:
                params["trigger"] = "index_price"
            elif block.trigger == Trigger.MARK:
                params["trigger"] = "mark_price"

        elif block.orderType == OrderType.TRAILING_STOP:
            self.logger.warning(("'trailing_stop' is not "
                                 "a valid order type for Deribit"))
            return

        if self.isPercent(block.quantity):
            if block.direction == Direction.BUY:
                price = ticker.get("best_ask_price")
            else:
                price = ticker.get("best_bid_price")

            quantity = price * self.referenceBalance(
                accountInfo.get("available_funds"), block.quantity)

            params["amount"] = self.toPrecise(quantity, 0)
        else:
            params["amount"] = self.toPrecise(block.quantity, 0)

        self.logger.debug("Trade method: %s, Params: %s", method, params)
        return self.getJsonMessage(method, params)

    def toPrecise(self, num, precision):
        decimals = max(0, int(precision))
        step = precision - decimals

        if (step):
            num = math.floor(num / step) * step

        return float(("{0:." + str(decimals) + "f}").format(float(num)))

    async def cancelOrder(self, websocket, alert, block):
        await websocket.send(self.getOpenOrdersJson(alert.symbol))
        response = await websocket.recv()
        j = json.loads(response)
        if self.isErrorResponse("get open order request", j):
            return
        orders = j.get("result")

        for order in orders:
            if (not block.direction or
                (block.direction == Direction.BUY and
                 order.get("direction") == "buy") or
                    (block.direction == Direction.SELL and
                     order.get("direction") == "sell")):
                await websocket.send(
                    self.getCancelJson(order.get("order_id")))
                response = await websocket.recv()
                j = json.loads(response)
                self.logResponse("canceling order", j)

    async def closePosition(self, websocket, precision, alert, block):
        # make sure previous trade completed. retry 5 times and then cancel
        for i in range(5):
            await websocket.send(self.getPositionJson(alert.symbol))
            response = await websocket.recv()
            j = json.loads(response)
            position = j.get("result")

            if position.get("trades") or position.get("order"):
                self.logger.warning(("Previous trade/order not completed, "
                                     "waiting 1 second and trying again. "
                                     "Attempt %s of 5"), i + 1)
                if i == 4:
                    self.logger.error(("Unable to execute close position, "
                                       "previous trade/order was not "
                                       "completed in time. "
                                       "5 attempts were made"))
                else:
                    time.sleep(1)
            else:
                break

        closeJson = self.getClosePositionJson(precision, block, position)
        if closeJson:
            await websocket.send(closeJson)
            response = await websocket.recv()
            j = json.loads(response)
            self.logResponse("close position", j)

    async def trade(self, websocket, precision, accountInfo, alert, block):
        await websocket.send(self.getTickerJson(alert.symbol))
        response = await websocket.recv()
        j = json.loads(response)
        if self.isErrorResponse("ticker request", j):
            return
        ticker = j.get("result")

        tradeJson = self.getTradeJson(
            precision, ticker, accountInfo, alert, block)
        if tradeJson:
            await websocket.send(tradeJson)
            response = await websocket.recv()
            j = json.loads(response)
            self.logResponse("trade", j)
