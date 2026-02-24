import uuid
from dataclasses import dataclass, field, replace
from typing import Self, Callable, TYPE_CHECKING

import gamelogs
from discord.ext import commands

if TYPE_CHECKING:
    from .stats import PlayerInfo


ROLES = [r for r in gamelogs.by_name.values() if r.default_faction in (gamelogs.town, gamelogs.coven)]

BUCKET_ALIASES = {
    "ti": "town investigative",
    "tk": "town killing",
    "tpow": "town power",
    "tp": "town protective",
    "ts": "town support",
    "to": "town outlier",
    "ct": "common town",
    "rt": "random town",

    "cd": "coven deception",
    "ck": "coven killing",
    "cpow": "coven power",
    "cu": "coven utility",
    "co": "coven outlier",
    "cc": "common coven",
    "rc": "random coven",
    "cov": "random coven",
    "coven": "random coven",

    "coro": "coroner",
    "inv": "investigator",
    "invest": "investigator",
    "lo": "lookout",
    "psy": "psychic",
    "sher": "sheriff",

    "dep": "deputy",
    "trick": "trickster",
    "vet": "veteran",
    "vig": "vigilante",
    "vigi": "vigilante",

    "marsh": "marshal",
    "mayo": "mayor",
    "mon": "monarch",
    "pros": "prosecutor",

    "bg": "bodyguard",
    "cler": "cleric",
    "crus": "crusader",
    "orac": "oracle",

    "admi": "admirer",
    "adm": "admirer",
    "amne": "amnesiac",
    "ret": "retributionist",
    "retri": "retributionist",
    "soc": "socialite",
    "soci": "socialite",
    "tav": "tavern keeper",

    "cata": "catalyst",
    "pil": "pilgrim",

    "dw": "dreamweaver",
    "ench": "enchanter",
    "illu": "illusionist",
    "dusa": "medusa",

    "conj": "conjurer",
    "rit": "ritualist",

    "cl": "coven leader",
    "hex": "hex master",
    "hm": "hex master",

    "necro": "necromancer",
    "pois": "poisoner",
    "pm": "potion master",
    "vm": "voodoo master",
    "wild": "wildling",
    "wl": "wildling",

    "cult": "cultist",
}

STRICT_BUCKETS = {
    **{b.casefold(): set(rs) for b, rs in gamelogs.by_bucket.items()},
    "common town": {r for r in ROLES if r.default_faction == gamelogs.town and gamelogs.bucket_of[r] != "Town Power"},
    "random town": {r for r in ROLES if r.default_faction == gamelogs.town},
    "common coven": {r for r in ROLES if r.default_faction == gamelogs.coven and gamelogs.bucket_of[r] not in ("Coven Killing", "Coven Power")},
    "random coven": {r for r in ROLES if r.default_faction == gamelogs.coven},
}

PURE_BUCKETS = {
    **{r.name.lower(): {r} for r in ROLES},
    **STRICT_BUCKETS,
}

BUCKETS = PURE_BUCKETS.copy()
for a, b in BUCKET_ALIASES.items():
    BUCKETS[a] = BUCKETS[b]


type Filter[T: IdentitySpecifier] = Callable[[T], T]

KEYWORDS: dict[str, Filter] = {
    "town": lambda spec: spec.with_faction(gamelogs.town),
    "green": lambda spec: spec.with_faction(gamelogs.town),
    "purple": lambda spec: spec.with_faction(gamelogs.coven),
    "tt": lambda spec: spec.where(lambda role: role.default_faction == gamelogs.town).with_faction(gamelogs.coven),
    "won": lambda spec: replace(spec, won=True),
    "hunt": lambda spec: replace(spec, hunt=True),
    **{n: lambda spec, rs=rs: spec.where(lambda role: role in rs) for n, rs in BUCKETS.items()},
}


@dataclass
class IdentitySpecifier:
    roles: list[gamelogs.Role] = field(default_factory=lambda: ROLES)
    faction: gamelogs.Faction | None = None
    won: bool | None = None
    hunt: bool | None = None
    only_starting: bool = field(default=False, kw_only=True)

    def __bool__(self) -> bool:
        return bool(self.roles)

    def to_sql(self) -> tuple[str, dict]:
        prefix = f"_{uuid.uuid4().hex}"
        clauses = []
        p = {}

        if len(self.roles) != len(ROLES):
            d = {f"{prefix}{i}": ident for i, ident in enumerate(self.roles)}
            marks = ",".join(":" + word for word in d)
            if self.only_starting:
                clauses.append(f"starting_role IN ({marks})")
            else:
                clauses.append(f"(starting_role IN ({marks}) OR ending_role IN ({marks}))")
            p.update(d)

        if self.faction is not None:
            clauses.append(f"faction = :{prefix}faction")
            p[f"{prefix}faction"] = self.faction
        if self.won is not None:
            clauses.append(f"won = :{prefix}won")
            p[f"{prefix}won"] = self.won
        if self.hunt is not None:
            clauses.append(f"saw_hunt = :{prefix}hunt")
            p[f"{prefix}hunt"] = self.hunt

        return f"({' AND '.join(clauses)})" if clauses else "(1)", p

    def where(self, f: Callable[[gamelogs.Role], bool]) -> Self:
        return replace(self, roles=[role for role in self.roles if f(role)])

    def with_faction(self, faction: gamelogs.Faction) -> Self:
        if self.faction is not None and faction != self.faction:
            return type(self)([])
        r = replace(self, faction=faction)
        return r.where(lambda r: r.default_faction == gamelogs.town) if faction == gamelogs.town else r

    async def finish_parsing(self, ctx: commands.Context, words: list[str]) -> None:
        if words:
            raise commands.BadArgument(f"I don't know what '{' '.join(words)}' means.")

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str) -> Self:
        us = cls()

        words = argument.split()
        while words:
            for izer in (lambda l: (-l, None), lambda l: (None, l)):
                for kw, f in KEYWORDS.items():
                    kw_parts = kw.split()
                    i, j = izer(len(kw_parts))
                    if [w.lower() for w in words[i:j]] == kw_parts and (n := f(us)) and n != us:
                        us = n
                        del words[i:j]
                        break
                else:
                    continue
                break
            else:
                break

        await us.finish_parsing(ctx, words)
        return us


@dataclass
class PlayerSpecifier(IdentitySpecifier):
    player: PlayerInfo | None = None
    name: str | None = None
    ign: str | None = None

    async def matches(self, game: gamelogs.GameResult, player: gamelogs.Player) -> bool:
        return (
            (player.starting_ident.role in self.roles or player.ending_ident.role in self.roles)
        and (self.faction is None or player.ending_ident.faction == self.faction)
        and (self.won is None or player.won == self.won)
        and (self.hunt is None or game.saw_hunt(player) == self.hunt)
        and (self.player is None or player.account_name.casefold() in [name.casefold() for name in await self.player.names()])
        and (self.name is None or player.account_name.casefold() == self.name.casefold())
        and (self.ign is None or player.game_name == self.ign)
        )

    def to_sql(self) -> tuple[str, dict]:
        prefix = f"_{uuid.uuid4().hex}"
        s, p = super().to_sql()
        clauses = [s]

        if self.player:
            clauses.append(f"player == :{prefix}player")
            p[f"{prefix}player"] = self.player.id
        if self.name:
            clauses.append(f"account_name == :{prefix}name")
            p[f"{prefix}name"] = self.name
        if self.ign:
            clauses.append(f"game_name == :{prefix}ign")
            p[f"{prefix}ign"] = self.ign

        return f"({' AND '.join(clauses)})", p

    async def finish_parsing(self, ctx: commands.Context, words: list[str]) -> None:
        from .stats import PlayerInfo

        for i, word in reversed(list(enumerate(words))):
            if word.startswith("ign:"):
                self.ign = " ".join([word.removeprefix("ign:"), *words[i+1:]]).strip().replace("\u200b", "")
                del words[i:]
                break

        title = " ".join(words)
        if title.startswith("account:"):
            self.name = title.removeprefix("account:").strip().replace("\u200b", "")
        elif title:
            self.player = await PlayerInfo.convert(ctx, title)
