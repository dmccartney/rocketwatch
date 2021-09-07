import json
import logging
import os

import discord
import termplotlib as tpl
from ens import ENS
from discord import Embed
from discord.ext import commands, tasks
from web3 import Web3

from strings import _
from utils.shorten import short_hex

log = logging.getLogger("rocketpool")
log.setLevel("DEBUG")


class RocketPool(commands.Cog):
  def __init__(self, bot):
    self.bot = bot
    self.loaded = True
    self.tnx_cache = []
    self.contracts = {}
    self.events = []
    self.mapping = {}

    infura_id = os.getenv("INFURA_ID")
    self.w3 = Web3(Web3.WebsocketProvider(f"wss://goerli.infura.io/ws/v3/{infura_id}"))
    temp_mainnet_w3 = Web3(Web3.WebsocketProvider(f"wss://mainnet.infura.io/ws/v3/{infura_id}"))
    self.ens = ENS.fromWeb3(temp_mainnet_w3)  # switch to self.w3 once we use mainnet

    with open("./data/rocketpool.json") as f:
      self.config = json.load(f)

    # load storage contract so we can dynamically load all required addresses
    storage = self.config['storage']
    with open(f"./contracts/{storage['name']}.abi", "r") as f:
      self.storage_contract = self.w3.eth.contract(address=storage["address"], abi=f.read())

    # Load Contracts and create Filters for all Events
    for name, events in self.config["sources"].items():
      address = self.get_address_from_storage_contract(name)
      with open(f"./contracts/{name}.abi", "r") as f:
        self.contracts[address] = self.w3.eth.contract(address=address, abi=f.read())
      for event in events:
        self.events.append(
          self.contracts[address].events[event].createFilter(fromBlock="latest", toBlock="latest"))
      self.mapping[address] = events
    if not self.run_loop.is_running():
      self.run_loop.start()

  def get_address_from_storage_contract(self, name):
    log.debug(f"retrieving address for {name}")
    sha3 = Web3.soliditySha3(["string", "string"], ["contract.address", name])
    return self.storage_contract.functions.getAddress(sha3).call()

  def get_pubkey_from_minipool(self, event):
    contract = self.contracts[event['address']]
    return contract.functions.getMinipoolPubkey(event["args"]["minipool"]).call()

  def get_proposal_info(self, event):
    contract = self.contracts[event['address']]
    result = {
      "message": contract.functions.getMessage(event["args"]["proposalID"]).call(),
      "votesFor": contract.functions.getVotesFor(event["args"]["proposalID"]).call() // 10 ** 18,
      "votesAgainst": contract.functions.getVotesAgainst(event["args"]["proposalID"]).call() // 10 ** 18,
    }
    return result

  def get_dao_member_name(self, member_address):
    address = self.get_address_from_storage_contract("rocketDAONodeTrusted")
    with open(f"./contracts/rocketDAONodeTrusted.abi", "r") as f:
      contract = self.w3.eth.contract(address=address, abi=f.read())
    return contract.functions.getMemberID(member_address).call()

  def create_embed(self, event_name, event):
    embed = Embed(color=discord.Color.from_rgb(235, 142, 85))
    embed.set_footer(text=os.getenv("CREDITS"), icon_url=os.getenv("CREDITS_ICON"))

    # prepare args
    args = dict(event['args'])

    # get pubkey of validator if a Minipool is involved
    if "minipool" in event_name:
      pubkey = self.get_pubkey_from_minipool(event)
      embed.add_field(name="validator",
                      value=f"[{short_hex(pubkey)}](https://prater.beaconcha.in/validator/{pubkey})",
                      inline=False)

    # add proposal message manually if the event contains a proposal
    if "proposal" in event_name:
      data = self.get_proposal_info(event)
      args["message"] = data["message"]
      # create bar graph for votes
      vote_graph = tpl.figure()
      vote_graph.barh([data["votesFor"], data["votesAgainst"]], ["For", "Against"], max_width=20)
      args["vote_graph"] = vote_graph.get_string()

    # create human readable Decision for Votes
    if "supported" in args:
      args["decision"] = "for" if args["supported"] else "against"

    for arg_key, arg_value in list(args.items()):
      if any(keyword in arg_key.lower() for keyword in ["amount", "value"]):
        args[arg_key] = arg_value / 10 ** 18

      if str(arg_value).startswith("0x"):
        name = ""
        if self.w3.isAddress(arg_value):
          name = self.ens.name(arg_value)
        if not name:
          name = f"{short_hex(arg_value)}"
        args[f"{arg_key}_fancy"] = f"[{name}](https://goerli.etherscan.io/search?q={arg_value})"

    # add oDAO member name if we can
    if "odao" in event_name:
      keys = [key for key in ["nodeAddress", "canceller", "executer", "proposer", "voter"] if key in args]
      if keys:
        key = keys[0]
        name = self.get_dao_member_name(args[key])
        if name:
          args["member_fancy"] = f"[{name}](https://goerli.etherscan.io/search?q={args[key]})"
        else:
          args["member_fancy"] = args[key + '_fancy']

    embed.title = _(f"rocketpool.{event_name}.title")
    embed.description = _(f"rocketpool.{event_name}.description", **args)

    tnx_hash = event['transactionHash'].hex()
    embed.add_field(name="Transaction Hash",
                    value=f"[{short_hex(tnx_hash)}](https://goerli.etherscan.io/tx/{tnx_hash})")

    if "from" in args:
      embed.add_field(name="Sender Address", value=args["from_fancy"])

    embed.add_field(name="Block Number",
                    value=f"[{event['blockNumber']}](https://goerli.etherscan.io/block/{event['blockNumber']})")
    return embed

  @tasks.loop(seconds=15.0)
  async def run_loop(self):
    if self.loaded:
      try:
        return await self.check_for_new_events()
      except Exception as err:
        self.loaded = False
        log.exception(err)
    try:
      return self.__init__(self.bot)
    except Exception as err:
      self.loaded = False
      log.exception(err)

  async def check_for_new_events(self):
    if not self.loaded:
      return
    log.debug("checking for new events")

    messages = []

    # Newest Event first so they are preferred over older ones.
    # Handles small reorgs better this way
    for events in self.events:
      for event in reversed(list(events.get_new_entries())):
        if event["event"] in self.mapping[event['address']]:

          # skip if we already have seen this message
          tnx_hash = event["transactionHash"]
          if tnx_hash in self.tnx_cache:
            continue

          event_name = self.mapping[event['address']][event["event"]]

          # lazy way of making it sort sensible within a single block
          score = event["blockNumber"] + (event["transactionIndex"] / 1000)

          messages.append({
            "score": score,
            "embed": self.create_embed(event_name, event),
            "event_name": event_name
          })

          # to prevent duplicate messages
          self.tnx_cache.append(tnx_hash)

          log.debug(event_name)
          print(event)
    log.debug("finished checking for new events")

    default_channel = await self.bot.fetch_channel(os.getenv("DEFAULT_CHANNEL"))
    odao_channel = await self.bot.fetch_channel(os.getenv("ODAO_CHANNEL"))
    for event in sorted(messages, key=lambda a: a["score"], reverse=False):
      if "odao" in event["event_name"]:
        await odao_channel.send(embed=event["embed"])
      else:
        await default_channel.send(embed=event["embed"])

    # this is so we don't just continue and use up more and more memory for the deduplication
    self.tnx_cache = self.tnx_cache[-1000:]

  def cog_unload(self):
    self.loaded = False
    self.run_loop.cancel()


def setup(bot):
  bot.add_cog(RocketPool(bot))
