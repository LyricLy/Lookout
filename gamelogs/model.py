from dataclasses import dataclass as _dataclass, field as _field
from typing import Literal as _Literal


@_dataclass(eq=False)
class Faction:
    name: str

    def __repr__(self):
        return self.name.lower().replace(" ", "_")

    def __str__(self):
        return self.name

unknown = Faction("Unknown")
town = Faction("Town")
coven = Faction("Coven")
apocalypse = Faction("Apocalypse")
arsonist = Faction("Arsonist")
serial_killer = Faction("Serial Killer")
shroud = Faction("Shroud")
werewolf = Faction("Werewolf")
vampire = Faction("Vampire")


@_dataclass(eq=False)
class Role:
    name: str
    default_faction: Faction | None

    def __repr__(self):
        return f"by_name[{self.name!r}]"

    def __str__(self):
        return self.name

by_bucket = {
    "Town Investigative": [
        Role("Coroner", town),
        Role("Investigator", town),
        Role("Lookout", town),
        Role("Psychic", town),
        Role("Seer", town),
        Role("Sheriff", town),
        Role("Spy", town),
        Role("Tracker", town),
    ],
    "Town Protective": [
        Role("Bodyguard", town),
        Role("Cleric", town),
        Role("Crusader", town),
        Role("Oracle", town),
        Role("Trapper", town),
    ],
    "Town Killing": [
        Role("Deputy", town),
        Role("Trickster", town),
        Role("Veteran", town),
        Role("Vigilante", town),
    ],
    "Town Support": [
        Role("Admirer", town),
        Role("Amnesiac", town),
        Role("Retributionist", town),
        Role("Socialite", town),
        Role("Tavern Keeper", town),
    ],
    "Town Power": [
        Role("Jailor", town),
        Role("Marshal", town),
        Role("Mayor", town),
        Role("Monarch", town),
        Role("Prosecutor", town),
    ],
    "Town Outlier": [
        Role("Catalyst", town),
        Role("Pilgrim", town),
    ],

    "Coven Power": [
        Role("Coven Leader", coven),
        Role("Hex Master", coven),
        Role("Witch", coven),
    ],
    "Coven Killing": [
        Role("Conjurer", coven),
        Role("Jinx", coven),
        Role("Ritualist", coven),
    ],
    "Coven Deception": [
        Role("Dreamweaver", coven),
        Role("Enchanter", coven),
        Role("Illusionist", coven),
        Role("Medusa", coven),
    ],
    "Coven Utility": [
        Role("Necromancer", coven),
        Role("Poisoner", coven),
        Role("Potion Master", coven),
        Role("Voodoo Master", coven),
        Role("Wildling", coven),
    ],
    "Coven Outlier": [
        Role("Covenite", coven),
        Role("Cultist", coven),
    ],

    "Neutral Evil": [
        Role("Doomsayer", None),
        Role("Executioner", None),
        Role("Jester", None),
        Role("Pirate", None),
    ],
    "Neutral Killing": [
        Role("Arsonist", arsonist),
        Role("Serial Killer", serial_killer),
        Role("Shroud", shroud),
        Role("Werewolf", werewolf),
    ],
    "Neutral Apocalypse": [
        Role("Baker", apocalypse),
        Role("Berserker", apocalypse),
        Role("Plaguebearer", apocalypse),
        Role("Soul Collector", apocalypse),
        Role("Famine", apocalypse),
        Role("War", apocalypse),
        Role("Pestilence", apocalypse),
        Role("Death", apocalypse),
    ],
    "Neutral Outlier": [
        Role("Cursed Soul", None),
        Role("Vampire", vampire),
    ],
}

by_name = {r.name: r for b in by_bucket.values() for r in b}
bucket_of = {r: n for n, b in by_bucket.items() for r in b}


@_dataclass(unsafe_hash=True)
class Identity:
    role: Role
    faction: Faction | None

    def __init__(self, role: Role, faction: Faction | None = unknown) -> None:
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "faction", faction if faction != unknown else role.default_faction)

    def __str__(self):
        if self.role.default_faction == self.faction:
            return f"{self.role}"
        else:
            return f"{self.role} ({self.faction})"

type Time = tuple[int, _Literal["day", "night"]]

@_dataclass(unsafe_hash=True)
class Player:
    number: int
    game_name: str
    account_name: str
    starting_ident: Identity = _field(compare=False)
    ending_ident: Identity = _field(compare=False)
    died: Time | None = _field(compare=False, default=None)
    won: bool = _field(compare=False, default=False)

    def __str__(self):
        ident = f"{self.ending_ident}" if self.starting_ident == self.ending_ident else f"{self.ending_ident} (originally {self.starting_ident})"
        return f"{' *'[self.won]}{' x'[bool(self.died)]} {f'[{self.number}]':>4} {self.account_name} as {self.game_name} - {ident}"

@_dataclass(unsafe_hash=True)
class GameResult:
    players: tuple[Player, ...]
    victor: Faction | None = _field(compare=False)
    hunt_reached: int | None = _field(compare=False)
    modifiers: list[str] = _field(compare=False)
    vip: Player | None = _field(compare=False)
    ended: Time

    def __str__(self):
        return "\n".join(map(str, self.players))
