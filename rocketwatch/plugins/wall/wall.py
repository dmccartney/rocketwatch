import asyncio
import contextlib
import logging
from datetime import datetime, timezone, timedelta
from io import BytesIO

import aiohttp
import humanize
import matplotlib.pyplot as plt
import numpy as np
from discord import File
from discord.ext import commands, tasks
from discord.ext.commands import Context
from discord.ext.commands import hybrid_command
from matplotlib import ticker
from matplotlib.ticker import AutoMinorLocator
from motor.motor_asyncio import AsyncIOMotorClient
from scipy.interpolate import interp1d

from utils import solidity
from utils.cfg import cfg
from utils.embeds import Embed
from utils.readable import s_hex
from utils.reporter import report_error
from utils.rocketpool import rp
from utils.thegraph import get_uniswap_pool_depth, get_uniswap_pool_stats
from utils.visibility import is_hidden_weak

log = logging.getLogger("wall")
log.setLevel(cfg["log_level"])


class Wall(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = AsyncIOMotorClient(cfg["mongodb_uri"]).get_database("rocketwatch")

        if not self.run_loop.is_running() and bot.is_ready():
            self.run_loop.start()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.run_loop.is_running():
            return
        self.run_loop.start()

    @tasks.loop(seconds=120)
    async def run_loop(self):
        try:
            await self.gather_new_data()
        except Exception as err:
            await report_error(err)

    async def cow_get_exchange(self, sell_token, buy_token, sell_amount):
        # this gets a quote from the cow api
        api_url = "https://api.cow.fi/mainnet/api/v1/quote"
        params = {
            "sellToken"          : sell_token,
            "buyToken"           : buy_token,
            "receiver"           : "0x0000000000000000000000000000000000000000",
            "appData"            : "0x0000000000000000000000000000000000000000000000000000000000000000",
            "partiallyFillable"  : False,
            "sellTokenBalance"   : "erc20",
            "buyTokenBalance"    : "erc20",
            "from"               : "0x0000000000000000000000000000000000000000",
            "signingScheme"      : "eip1271",
            "onchainOrder"       : True,
            "kind"               : "sell",
            "sellAmountBeforeFee": str(int(sell_amount*10**18))
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=params) as resp:
                data = await resp.json()
        try:
            return solidity.to_float(data["quote"]["buyAmount"])
        except KeyError:
            raise Exception(f"Could not get sell depth for {sell_amount} RPL, json: {data}")

    async def cow_get_effective_exchange_rate_using_delta(self, sell_token, buy_token, sell_amount):
        # this method calculates the effective exchange rate using the delta between the 2 token exchanges close by
        scale = max(1, sell_amount / 100)
        # gather surrounding quotes
        numbers = await asyncio.gather(
            self.cow_get_exchange(sell_token, buy_token, sell_amount - scale),
            self.cow_get_exchange(sell_token, buy_token, sell_amount + scale)
        )
        delta = numbers[1] - numbers[0]
        delta_scale = sell_amount / (scale * 2)
        effective_buy_amount = delta * delta_scale
        effective_ratio = sell_amount / effective_buy_amount
        # calculate the effective exchange rate
        return {
            "sell_amount": sell_amount,
            "buy_amount" : effective_buy_amount,
            "rate"       : effective_ratio
        }

    async def gather_new_data(self):
        # gather depth sell / buy values
        tmp = []
        ts = datetime.utcnow()
        rpl_address = rp.get_address_by_name("rocketTokenRPL")
        weth_address = rp.get_address_by_name("wrappedETH")
        d_ref = await self.cow_get_exchange(weth_address, rpl_address, 1)
        reference_point = 1 / d_ref

        for amount in [*list(range(10, 300, 30)), *list(range(100, 2000, 100))]:
            # get rate
            try:
                d = await self.cow_get_effective_exchange_rate_using_delta(weth_address, rpl_address, amount)
            except Exception as err:
                log.exception(err)
                continue
            # get slippage
            slippage = (d["rate"] - reference_point) / reference_point
            log.debug(f"Selling {amount} ETH for RPL at {d['rate']} ({slippage})")
            # store data
            tmp.append({
                "ts"         : ts,
                "sell_amount": d["sell_amount"],
                "sell_token" : "weth",
                "buy_amount" : d["buy_amount"],
                "buy_token"  : "rpl",
                "rate"       : d["rate"],
                "slippage"   : slippage
            })
        # get buy depth for 10, 100, 1000, 10000, 100000, 1000000 RPL but like in ETH
        # flip sell and buy token
        for amount in [*list(range(1000, 30000, 3000)), *list(range(30000, 200000, 10000))]:
            # get rate
            try:
                d = await self.cow_get_effective_exchange_rate_using_delta(rpl_address, weth_address, amount)
            except Exception as err:
                log.exception(err)
                continue
            # invert rate
            d["rate"] = 1 / d["rate"]
            # get slippage
            slippage = (d["rate"] - reference_point) / reference_point
            log.debug(f"Selling {amount} RPL for ETH at {d['rate']} ({slippage})")
            # store data
            tmp.append({
                "ts"         : ts,
                "sell_amount": d["sell_amount"],
                "sell_token" : "rpl",
                "buy_amount" : d["buy_amount"],
                "buy_token"  : "weth",
                "rate"       : d["rate"],
                "slippage"   : slippage
            })
        # store data in db
        await self.db["wall"].insert_many(tmp)

    @hybrid_command()
    async def depth(self, ctx: Context):
        await ctx.defer(ephemeral=is_hidden_weak(ctx))
        # get latest data from db
        latest_ts = await self.db["wall"].find_one(sort=[("ts", -1)])
        if latest_ts is None:
            return await ctx.send("No data available")
        latest_ts = latest_ts["ts"]
        # make ts utc aware
        latest_ts = latest_ts.replace(tzinfo=timezone.utc)
        # get data from db
        data = await self.db["wall"].find({"ts": latest_ts}).to_list(length=None)
        # create plot using matplotlib. x axis should be slippage, y axis should be amount. use 2 y axis, one for sell amount, one for buy amount
        fig, ax = plt.subplots()
        # sell amount
        x_green = [x["slippage"] for x in data if x["sell_token"] == "weth"]
        x_green.insert(0, 0)
        y_green = [x["sell_amount"] for x in data if x["sell_token"] == "weth"]
        y_green.insert(0, 0)
        # interpolate
        x_green, y_green = zip(*sorted(zip(x_green, y_green)))
        # turn into dict and back to list to remove duplicates
        tmp = list(dict(zip(x_green, y_green)).items())
        x_green, y_green = zip(*tmp)
        inter_green = interp1d(x_green, y_green, kind="linear")
        x_green_inter = [x / 100 for x in range(26)]
        y_green_inter = inter_green(x_green_inter)
        ax.plot(x_green_inter, y_green_inter, color="green")
        # fill
        ax.fill_between(x_green_inter, y_green_inter, color="green", alpha=0.2)
        # debug: plot the raw data
        # ax.plot(x_green, y_green, color="green", marker="+", linestyle="")

        # buy amount
        x_red = [x["slippage"] for x in data if x["buy_token"] == "weth"]
        x_red.insert(0, 0)
        y_red = [x["buy_amount"] for x in data if x["buy_token"] == "weth"]
        y_red.insert(0, 0)
        # interpolate
        x_red, y_red = zip(*sorted(zip(x_red, y_red)))
        # turn into dict and back to list to remove duplicates
        tmp = list(dict(zip(x_red, y_red)).items())
        x_red, y_red = zip(*tmp)
        inter_red = interp1d(x_red, y_red, kind="linear")
        x_red_inter = [-x / 100 for x in range(26)]
        y_red_inter = inter_red(x_red_inter)
        ax.plot(x_red_inter, y_red_inter, color="red")
        # fill
        ax.fill_between(x_red_inter, y_red_inter, color="red", alpha=0.2)
        # debug: plot the raw data
        # ax.plot(x_red, y_red, color="red", marker="+", linestyle="")

        # set labels
        ax.set_xlabel("Slippage")
        ax.set_ylabel("ETH")
        # enable subticks for x axis
        ax.xaxis.set_minor_locator(AutoMinorLocator())
        # show x axis grid lines, also for minor ticks
        ax.grid()
        # format y axis with thousands separator
        ax.get_yaxis().set_major_formatter(ticker.FuncFormatter(lambda x, p: format(int(x), ',')))
        # format x axis with percentage
        ax.get_xaxis().set_major_formatter(ticker.PercentFormatter(xmax=1))
        # set x limit to -100% and 100%
        ax.set_xlim(-0.25, 0.25)
        ax.set_ylim(0, None)

        # use minimal whitespace
        plt.tight_layout()

        # store the graph in an file object
        file = BytesIO()
        # make sure to increase the dpi to make the graph look better
        plt.savefig(file, format='png', dpi=300)
        file.seek(0)

        # clear plot from memory
        plt.clf()
        plt.close()

        e = Embed()
        e.title = "RPL Sell and Buy depth"
        e.description = f"Data from <t:{int(latest_ts.timestamp())}:R>"
        e.set_image(url="attachment://depth.png")
        await ctx.send(file=File(file, "depth.png"), embed=e)

    @hybrid_command()
    async def wall(self, ctx: Context):
        """
        Show the current limit order sell wall on 1inch
        """
        await ctx.defer(ephemeral=is_hidden_weak(ctx))
        wall_address = "0xD779bB0F68F54f7521aA5b35dD88352771843764"
        rpl = rp.get_address_by_name("rocketTokenRPL").lower()
        """
        url = f"https://limit-orders.1inch.io/v3.0/1/limit-order/address/{wall_address}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
        """
        data = []
        total_volume_left = 0
        total_volume_rpl = 0
        maker_rate_min = 1
        maker_rate_max = 0
        for d in data:
            if d["data"]["makerAsset"] != rpl:
                continue
            total_volume_left += solidity.to_float(d["remainingMakerAmount"])
            total_volume_rpl += solidity.to_float(d["data"]["makingAmount"])
            rate = float(d["makerRate"])
            if rate < maker_rate_min:
                maker_rate_min = rate
            if rate > maker_rate_max:
                maker_rate_max = rate

        e = Embed()
        if total_volume_left == 0:
            # fallback to alternative method
            try:
                alternative = self._get_alternative_wall()
                e.set_image(url="attachment://wall.png")
                await ctx.send(
                    embed=e,
                    file=File(
                        alternative, filename="wall.png"
                    ))
                return
            except Exception as err:
                log.exception(err)
            e.set_image(url="https://media1.giphy.com/media/hEc4k5pN17GZq/giphy.gif")
            await ctx.send(embed=e)
            return

        rpl_balance = 0
        with contextlib.suppress(Exception):
            resp = rp.multicall.aggregate(
                rp.get_contract_by_name(name).functions.balanceOf(wall_address)
                for name in ["rocketTokenRPL", "rocketTokenRPLFixedSupply"]
            )
            for token in resp.results:
                contract_name = rp.get_name_by_address(token.contract_address)
                if "RPL" in contract_name:
                    rpl_balance += solidity.to_float(token.results[0])

        e.title = "1inch Sell Wall"
        e.set_author(name="🔗 Data from 1inch.io", url="https://1inch.io/")
        percent = 100 * total_volume_left / total_volume_rpl
        e.add_field(
            name="Liquidity", value=f"{humanize.intcomma(total_volume_left, 0)} RPL"
        )
        e.add_field(
            name="Range", value=f"{maker_rate_min:,.4f} - {maker_rate_max:,.4f}"
        )
        e.add_field(name="Status", value=f"{percent:,.2f}% left", inline=False)
        e.add_field(name="Wallet RPL Balance", value=humanize.intcomma(rpl_balance, 0))
        e.add_field(name="Wallet Address", value=f"[{s_hex(wall_address)}](https://rocketscan.io/address/{wall_address})")
        await ctx.send(embed=e)

    def _get_alternative_wall(self):
        # test the get_uniswap_pool_depth function
        a = get_uniswap_pool_depth("0xe42318ea3b998e8355a3da364eb9d48ec725eb45")
        # get current price from the pool stats
        sqrt_price = get_uniswap_pool_stats("0xe42318ea3b998e8355a3da364eb9d48ec725eb45")["sqrtPrice"]
        price = 1 / (int(sqrt_price) ** 2 / 2 ** 192)

        plt.plot([x[0] for x in a], [x[1] for x in a], drawstyle="steps-pre", color="black", linewidth=1)

        # color everything above the current tick red, below green
        # get the closest tick to the current price
        idx = min(range(len(a)), key=lambda i: abs(a[i][0] - price))
        above = a[idx:]
        below = a[:idx + 1]
        # plot the two lists
        plt.fill_between([x[0] for x in above], [x[1] for x in above], color="red", alpha=0.5, interpolate=False, step="pre")
        plt.fill_between([x[0] for x in below], [x[1] for x in below], color="green", alpha=0.5, interpolate=False, step="pre")
        # plot the current price as a vertical line
        plt.axvline(price, color="black", linestyle="--", linewidth=1)

        # hide y axis
        plt.gca().axes.get_yaxis().set_visible(False)

        # minor ticks for the x axis
        plt.gca().xaxis.set_minor_locator(plt.MultipleLocator(0.001))

        # set x axis ticks to numbers
        plt.gca().xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.3f}"))

        # set y axis min to 0
        plt.ylim(bottom=0)

        # vertical grid lines
        plt.gca().xaxis.grid(True, which="major", linestyle="--")
        plt.gca().xaxis.grid(True, which="minor", linestyle=":")

        # center the plot around the current price, make the x axis 4x as wide
        plt.xlim(price * 0.25, price * 1.75)

        # use minimal whitespace
        plt.tight_layout()

        # store the graph in an file object
        file = BytesIO()
        # make sure to increase the dpi to make the graph look better
        plt.savefig(file, format='png', dpi=300)
        file.seek(0)

        # clear plot from memory
        plt.clf()
        plt.close()

        return file


async def setup(bot):
    await bot.add_cog(Wall(bot))
