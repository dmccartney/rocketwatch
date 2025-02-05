import contextlib
import datetime
import logging
import math

import discord
import humanize
from discord import Color
from etherscan_labels import Addresses

from strings import _
from utils import readable
from utils.cached_ens import CachedEns
from utils.cfg import cfg
from utils.readable import cl_explorer_url, advanced_tnx_url, s_hex
from utils.rocketpool import rp
from utils.sea_creatures import get_sea_creature_for_address
from utils.shared_w3 import w3

ens = CachedEns()

log = logging.getLogger("embeds")
log.setLevel(cfg["log_level"])


class Embed(discord.Embed):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.colour = Color.from_rgb(235, 142, 85)
        self.set_footer_parts([])

    def set_footer_parts(self, parts):
        footer_parts = ["Developed by 0xinvis.eth",
                        "/donate"]
        if cfg["rocketpool.chain"] != "mainnet":
            footer_parts.insert(-1, f"Chain: {cfg['rocketpool.chain'].capitalize()}")
        footer_parts.extend(parts)
        self.set_footer(text=" · ".join(footer_parts))


def el_explorer_url(target, name="", prefix="", make_code=False):
    url = f"https://{cfg['rocketpool.execution_layer.explorer']}/search?q={target}"
    if w3.isAddress(target):
        # sanitize address
        target = w3.toChecksumAddress(target)
        # rocketscan url stuff
        if cfg["rocketpool.chain"] == "mainnet":
            if rp.call("rocketMinipoolManager.getMinipoolExists", target):
                if cfg["rocketpool.chain"] == "goerli":
                    url = f"https://prater.rocketscan.io/minipool/{target}"
                else:
                    url = f"https://rocketscan.io/minipool/{target}"
            if rp.call("rocketNodeManager.getNodeExists", target):
                if rp.call("rocketNodeManager.getSmoothingPoolRegistrationState", target) and prefix != -1:
                    prefix += ":cup_with_straw:"
                if cfg["rocketpool.chain"] == "goerli":
                    url = f"https://prater.rocketscan.io/node/{target}"
                else:
                    url = f"https://rocketscan.io/node/{target}"

        n_key = f"addresses.{target}"
        if not name and (n := _(n_key)) != n_key:
            name = n

        if cfg["rocketpool.chain"] != "mainnet" and not name:
            name = s_hex(target)

        if not name and (member_id := rp.call("rocketDAONodeTrusted.getMemberID", target)):
            if prefix != -1:
                prefix += "🔮"
            name = member_id
        if not name:
            a = Addresses.get(target)
            # don't apply name if its only label is one with the id "take-action", as these don't show up on the explorer
            if not a.labels or len(a.labels) != 1 or a.labels[0].id != "take-action":
                name = a.name
        if not name:
            # not an odao member, try to get their ens
            name = ens.get_name(target)

        if code := w3.eth.get_code(target):
            if prefix != -1:
                prefix += "📄"
            if (
                    not name
                    and w3.keccak(text=code.hex()).hex()
                    in cfg["mev.hashes"]
            ):
                name = "MEV Bot Contract"
            if not name:
                with contextlib.suppress(Exception):
                    c = w3.eth.contract(address=target, abi=[{"inputs"         : [],
                                                              "name"           : "name",
                                                              "outputs"        : [{"internalType": "string",
                                                                                   "name"        : "",
                                                                                   "type"        : "string"}],
                                                              "stateMutability": "view",
                                                              "type"           : "function"}])
                    n = c.functions.name().call()
                    # make sure nobody is trying to inject a custom link, as there was a guy that made the name of his contract
                    # 'RocketSwapRouter](https://etherscan.io/search?q=0x16d5a408e807db8ef7c578279beeee6b228f1c1c)[',
                    # in an attempt to get people to click on his contract

                    # first, if the name has a link in it, we ignore it
                    if any(keyword in n.lower() for keyword in
                           ["http", "discord", "airdrop", "telegram", "twitter", "youtube"]):
                        log.warning(f"Contract {target} has a suspicious name: {n}")
                    else:
                        name = f"{discord.utils.remove_markdown(n, ignore_links=False)}*"

    if not name:
        # fall back to shortened address
        name = s_hex(target)
    if make_code:
        name = f"`{name}`"
    if prefix == -1:
        prefix = ""
    return f"{prefix}[{name}]({url})"


def prepare_args(args):
    for arg_key, arg_value in list(args.items()):
        # store raw value
        args[f"{arg_key}_raw"] = arg_value

        # handle numbers
        if any(keyword in arg_key.lower() for keyword in ["amount", "value", "rate", "totaleth", "stakingeth", "rethsupply", "rplprice"]) and isinstance(arg_value, int):
            args[arg_key] = arg_value / 10 ** 18

        # handle timestamps
        if "deadline" in arg_key.lower() and isinstance(arg_value, int):
            args[arg_key] = f"<t:{arg_value}:f>(<t:{arg_value}:R>)"

        # handle percentages
        if "perc" in arg_key.lower():
            args[arg_key] = arg_value / 10 ** 16
        if arg_key.lower() in ["rate", "penalty"]:
            args[f"{arg_key}_perc"] = arg_value / 10 ** 16

        # handle hex strings
        if str(arg_value).startswith("0x"):
            prefix = ""

            if w3.isAddress(arg_value):
                # get rocketpool related holdings value for this address
                address = w3.toChecksumAddress(arg_value)
                prefix = get_sea_creature_for_address(address)

            # handle validators
            if arg_key == "pubkey":
                args[arg_key] = cl_explorer_url(arg_value)
            elif arg_key == "cow_uid":
                args[arg_key] = f"[ORDER](https://explorer.cow.fi/orders/{arg_value})"
            else:
                args[arg_key] = el_explorer_url(arg_value, prefix=prefix)
                args[f'{arg_key}_clean'] = el_explorer_url(arg_value)
                if len(arg_value) == 66:
                    args[f'{arg_key}_small'] = el_explorer_url(arg_value, name="[tnx]")
    if "from" in args:
        args["fancy_from"] = args["from"]
        if "caller" in args and args["from"] != args["caller"]:
            args["fancy_from"] = f"{args['caller']} ({args['from']})"
    return args


def assemble(args):
    e = Embed()
    if args.event_name in ["service_interrupted", "finality_delay_event"]:
        e.colour = Color.from_rgb(235, 86, 86)
    if "sell_rpl" in args.event_name:
        e.colour = Color.from_rgb(235, 86, 86)
    if "buy_rpl" in args.event_name or "finality_delay_recover_event" in args.event_name:
        e.colour = Color.from_rgb(86, 235, 86)

    do_small = all([
        _(f"embeds.{args.event_name}.description_small") != f"embeds.{args.event_name}.description_small",
        args.get("amount" if "ethAmount" not in args else "ethAmount", 0) < 100])

    if not do_small:
        e.title = _(f"embeds.{args.event_name}.title")

    if "pool_deposit" in args.event_name and args.get("amount" if "ethAmount" not in args else "ethAmount", 0) >= 1000:
        e.set_image(url="https://media.giphy.com/media/VIX2atZr8dCKk5jF6L/giphy.gif")

    # make numbers look nice
    for arg_key, arg_value in list(args.items()):
        if any(keyword in arg_key.lower() for keyword in
               ["amount", "value", "total_supply", "perc", "tnx_fee", "rate"]):
            if not isinstance(arg_value, (int, float)) or "raw" in arg_key:
                continue
            if arg_value:
                decimal = 5 - math.floor(math.log10(abs(arg_value)))
                decimal = max(0, min(5, decimal))
                arg_value = round(arg_value, decimal)
            if arg_value == int(arg_value):
                arg_value = int(arg_value)
            args[arg_key] = humanize.intcomma(arg_value)

    if do_small:
        e.description = _(f"embeds.{args.event_name}.description_small", **args)
        if cfg["rocketpool.chain"] != "mainnet":
            e.description += f" ({cfg['rocketpool.chain'].capitalize()})"
        e.set_footer(text="")
        return e

    e.description = _(f"embeds.{args.event_name}.description", **args)

    if "cow_uid" in args:
        e.add_field(name="Cow Order",
                    value=args.cow_uid,
                    inline=False)

    if "exchangeRate" in args:
        e.add_field(name="Exchange Rate",
                    value=f"`{args.exchangeRate} RPL/{args.otherToken}`" +
                          (
                              f" (`{args.discountAmount}%` Discount, oDAO: `{args.marketExchangeRate} RPL/ETH`)" if "discountAmount" in args else ""),
                    inline=False)

    """
    # show public key if we have one
    if "pubkey" in args:
        e.add_field(name="Validator",
                    value=args.pubkey,
                    inline=False)
    """

    if "epoch" in args:
        e.add_field(name="Epoch",
                    value=f"[{args.epoch}](https://{cfg['rocketpool']['consensus_layer']['explorer']}/epoch/{args.epoch})")

    if "timezone" in args:
        e.add_field(name="Timezone",
                    value=f"`{args.timezone}`",
                    inline=False)

    if "node_operator" in args:
        e.add_field(name="Node Operator",
                    value=args.node_operator)

    if "slashing_type" in args:
        e.add_field(name="Reason",
                    value=f"`{args.slashing_type} Violation`")

    """
    if "commission" in args:
        e.add_field(name="Commission Rate",
                    value=f"{args.commission:.2%}",
                    inline=False)
    """

    if "settingContractName" in args:
        e.add_field(name="Contract",
                    value=f"`{args.settingContractName}`",
                    inline=False)

    if "invoiceID" in args:
        e.add_field(name="Invoice ID",
                    value=f"`{args.invoiceID}`",
                    inline=False)

    if "contractAddress" in args and "Contract" in args.type:
        e.add_field(name="Contract Address",
                    value=args.contractAddress,
                    inline=False)

    if "url" in args:
        e.add_field(name="URL",
                    value=args.url,
                    inline=False)

    # show current inflation
    if "inflation" in args:
        e.add_field(name="Current Inflation",
                    value=f"{args.inflation}%",
                    inline=False)

    if "submission" in args and "merkleTreeCID" in args.submission:
        n = f"0x{s_hex(args.submission.merkleRoot.hex())}"
        e.add_field(name="Merkle Tree",
                    value=f"[{n}](https://gateway.ipfs.io/ipfs/{args.submission.merkleTreeCID})")

    # show transaction hash if possible
    if "transactionHash" in args:
        content = f"{args.transactionHash}{advanced_tnx_url(args.transactionHash_raw)}"
        e.add_field(name="Transaction Hash",
                    value=content)

    # show sender address
    if senders := [value for key, value in args.items() if key.lower() in ["sender", "from"]]:
        sender = senders[0]
        v = sender
        # if args["origin"] is an address and does not match the sender, show both
        if "caller" in args and args["caller"] != sender and "0x" in args["caller"]:
            v = f"{args.caller} ({sender})"
        e.add_field(name="Sender Address",
                    value=v)

    # show block number
    if "blockNumber" in args:
        e.add_field(name="Block Number",
                    value=f"[{args.blockNumber}](https://etherscan.io/block/{args.blockNumber})")

    if "slot" in args:
        e.add_field(name="Slot",
                    value=f"[{args.slot}](https://beaconcha.in/slot/{args.slot})")

    if "smoothie_amount" in args:
        e.add_field(name="Smoothing Pool Balance",
                    value=f"||{args.smoothie_amount}|| ETH")

    if "reason" in args and args["reason"]:
        e.add_field(name="Likely Revert Reason",
                    value=f"`{args.reason}`",
                    inline=False)

    # show timestamp
    if "time" in args.keys():
        times = [args["time"]]
    else:
        times = [value for key, value in args.items() if "time" in key.lower()]
    time = times[0] if times else int(datetime.datetime.now().timestamp())
    e.add_field(name="Timestamp",
                value=f"<t:{time}:R> (<t:{time}:f>)",
                inline=False)

    # show the transaction fees
    if "tnx_fee" in args:
        e.add_field(name="Transaction Fee",
                    value=f"{args.tnx_fee} ETH ({args.tnx_fee_dai} DAI)",
                    inline=False)

    if "_slash_" in args.event_name or "finality_delay_event" in args.event_name:
        e.set_image(url="https://c.tenor.com/p3hWK5YRo6IAAAAC/this-is-fine-dog.gif")

    if "_proposal_smoothie_" in args.event_name:
        e.set_image(url="https://cdn.discordapp.com/attachments/812745786638336021/1106983677130461214/butta-commie-filter.png")

    return e
