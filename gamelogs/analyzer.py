from __future__ import annotations

from collections import defaultdict

from .errors import UnsupportedRoleError, NotLogError
from .messages import *
from .model import *


class Analyzer[R]:
    def get_message(self, message: Message) -> None:
        raise NotImplementedError()

    def result(self) -> R:
        raise NotImplementedError()

    def __and__[R2](self, other: Analyzer[R2]) -> ZipAnalyzer[R, R2]:
        return ZipAnalyzer(self, other)

class ZipAnalyzer[R1, R2](Analyzer[tuple[R1, R2]]):
    def __init__(self, x: Analyzer[R1], y: Analyzer[R2]):
        self.x = x
        self.y = y

    def get_message(self, message: Message) -> None:
        self.x.get_message(message)
        self.y.get_message(message)

    def result(self) -> tuple[R1, R2]:
        return self.x.result(), self.y.result()


class MessageCountAnalyzer(Analyzer[int]):
    def __init__(self):
        self.count = 0

    def get_message(self, message: Message) -> None:
        self.count += 1

    def result(self) -> int:
        return self.count


def is_evil_raiser(ident: Identity) -> bool:
    return ident.role.name == "Necromancer" or ident.role.name == "Retributionist" and ident.faction != town

class ResultAnalyzer(Analyzer[GameResult]):
    def __init__(self) -> None:
        self.players = {}
        self.townie_colours = []
        self.coven_colour = None
        self.dced = set()
        self.hunt_reached = None
        self.in_trib = False
        self.trial_period = False
        self.time = 1, "day"
        self.modifiers = []
        self.death_popped = False
        self.draw_tomorrow = False
        self.vip = None

    def judge_miscoloured_townies(self) -> None:
        for player, colour in self.townie_colours:
            if colour == self.coven_colour:
                player.starting_ident.faction = coven
                player.ending_ident.faction = coven
                self.modifiers.append("Town Traitor")

    def kill(self, who: str, *, last_night: bool = False) -> None:
        player = self.players[who]
        if not player.died:
            self.draw_tomorrow = False
            player.died = (self.time[0]-1, "night") if last_night else self.time

    def get_message(self, message: Message) -> None:
        match message:
            case PlayerInfo(number, game_name, account_name, role, prev_role, _, is_vip):
                try:
                    ending_ident = Identity(by_name[role[0]])
                    starting_ident = Identity(by_name[prev_role[0]]) if prev_role else ending_ident
                except KeyError as e:
                    raise UnsupportedRoleError(e.args[0])
                faction_colour_shown = ending_ident.faction
                if ending_ident.role.name != "Vampire":
                    ending_ident.faction = starting_ident.faction
                player = Player(number, game_name, account_name, starting_ident, ending_ident)
                if is_vip:
                    self.vip = player
                    self.modifiers.append("VIP")
                self.players[game_name] = player
                if faction_colour_shown == coven:
                    self.coven_colour = role[1]
                elif faction_colour_shown == town:
                    self.townie_colours.append((player, role[1]))
            case LeftAWill(who):
                # sometimes we miss death messages so this is an imperfect backup
                self.kill(who)
            case Upped(who):
                if self.in_trib:
                    self.kill(who)
            case NightDeath(who):
                self.kill(who, last_night=True)
            case DayDeath(who):
                self.kill(who)
            case FoundGuilty(who):
                player = self.players[who]
                if player.ending_ident.role.name == "Jester":
                    player.won = True
                self.kill(who)
            case DayStart(1):
                self.judge_miscoloured_townies()
            case DayStart(n):
                self.time = n, "day"
                self.trial_period = False
                for dc in self.dced:
                    self.kill(dc, last_night=True)
                self.dced.clear()
            case NightStart(n):
                self.time = n, "night"
                self.death_popped = False
                self.in_trib = False
            case LeftTown(who, _):
                them = self.players[who]
                if them.starting_ident.role.name == "Cursed Soul":
                    for player in self.players.values():
                        if player.starting_ident.role.name == "Cursed Soul":
                            player.won = True
                else:
                    them.won = True
            case HuntWarning(days_left):
                if not self.hunt_reached:
                    self.hunt_reached = self.time[0] + days_left - 3
            case Tribunal():
                self.in_trib = True
            case Disconnect(who):
                self.dced.add(who)
            case Reconnect(who):
                try:
                    self.dced.remove(who)
                except KeyError:
                    # shit. they're still alive
                    self.players[who].died = None
            case TrialsRemaining() if not self.trial_period:
                self.trial_period = True
            case DeathPop():
                self.death_popped = True
            case DrawWarning():
                self.draw_tomorrow = True

    def result(self) -> GameResult:
        if len(self.players) < 5:
            raise NotLogError("input is not a gamelog")

        victor = unknown

        in_game = set([x.ending_ident.faction for x in self.players.values() if not x.died])
        in_game.discard(None)
        if not in_game:
            # a draw
            victor = None
        elif len(in_game) == 1:
            victor = in_game.pop()

        if victor == unknown:
            if self.death_popped:
                # Death win
                victor = apocalypse
            elif self.hunt_reached == self.time[0] - 3:
                # hunt win
                victor = coven
            elif self.draw_tomorrow:
                # draw, no deaths
                victor = None
            elif (
                in_game == {town, coven} and
                len(last_town := [x for x in self.players.values() if not x.died and x.ending_ident.faction == town]) == 1 and
                any([x.starting_ident.role.name == "Cultist" for x in self.players.values()])
            ):
                # last town is indoctrinated
                for townie in last_town:
                    townie.ending_ident.faction = coven
                victor = coven
            elif (
                in_game == {town, vampire} and
                len(last_town := [x for x in self.players.values() if not x.died and x.ending_ident.faction == town]) <= 3
            ):
                # last town are converted
                for townie in last_town:
                    townie.ending_ident.faction = vampire
                victor = vampire
            else:
                # HM win?
                for hm in self.players.values():
                    if hm.ending_ident.role.name == "Hex Master":
                        if not hm.died or any([not x.died and is_evil_raiser(x.ending_ident) for x in self.players.values()]):
                            victor = coven
                        break

        for player in self.players.values():
            if player.ending_ident.faction == victor:
                player.won = True

        return GameResult(tuple(self.players.values()), victor, self.hunt_reached, self.modifiers, self.vip, self.time)
