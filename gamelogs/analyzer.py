from collections import defaultdict

from lxml.html import tostring

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
    def __init__(self, x: Analyzer[R1], y: Analyzer[R2]) -> None:
        self.x = x
        self.y = y

    def get_message(self, message: Message) -> None:
        self.x.get_message(message)
        self.y.get_message(message)

    def result(self) -> tuple[R1, R2]:
        return self.x.result(), self.y.result()


class MessageCountAnalyzer(Analyzer[int]):
    def __init__(self) -> None:
        self.count = 0

    def get_message(self, message: Message) -> None:
        self.count += 1

    def result(self) -> int:
        return self.count


def is_evil_raiser(ident: Identity) -> bool:
    return ident.role == by_name("Necromancer") or ident.role == by_name("Retributionist") and ident.faction != town

class ResultAnalyzer(Analyzer[GameResult]):
    def __init__(self, *, pandora: bool = False) -> None:
        self.players = {}
        self.townie_colours = defaultdict(list)
        self.dced = set()
        self.hunt_reached = None
        self.in_trib = False
        self.time = DayTime()
        self.modifiers = []
        self.death_popped = False
        self.draw_tomorrow = False
        self.vip = None
        self.pandora = pandora

    def judge_miscoloured_townies(self) -> None:
        if len(self.townie_colours) != 2:
            return
        for colour, (player, *others) in self.townie_colours.items():
            if not others:
                player.starting_ident.faction = coven
                player.starting_ident.tt = True
                player.ending_ident.faction = coven
                player.ending_ident.tt = True
                self.modifiers.append("Town Traitor")
                break

    def kill(self, who: str, *, last_night: bool = False, hanged: HangCause | None = None, dced: bool = False) -> None:
        player = self.players[who]

        if hanged:
            player.hanged = hanged
            if player.ending_ident.role == by_name("Jester"):
                player.won = True
        if dced:
            player.dced = True

        if not player.died:
            self.draw_tomorrow = False
            player.died = DayTime(self.time.day-1, Time.NIGHT) if last_night else self.time

    def get_message(self, message: Message) -> None:
        match message:
            case PlayerInfo(number, game_name, account_name, role, prev_role, will_tree, is_vip):
                try:
                    ending_ident = Identity(by_name(role[0]))
                    starting_ident = Identity(by_name(prev_role[0])) if prev_role else ending_ident
                except KeyError as e:
                    raise UnsupportedRoleError(e.args[0])
                if self.pandora and starting_ident.faction == apocalypse:
                    starting_ident.faction = coven
                faction_colour_shown = ending_ident.faction
                if ending_ident.role != by_name("Vampire"):
                    ending_ident.faction = starting_ident.faction
                will = (will_tree.text or "") + "".join([tostring(c, encoding="unicode") for c in will_tree.getchildren()]) if will_tree is not None else None
                player = Player(number, game_name, account_name, starting_ident, ending_ident, will)
                if is_vip:
                    self.vip = player
                    self.modifiers.append("VIP")
                self.players[game_name] = player
                if faction_colour_shown == town:
                    self.townie_colours[role[1]].append(player)
            case LeftAWill(who):
                # sometimes we miss death messages so this is an imperfect backup
                self.kill(who)
            case Upped(who):
                if self.in_trib:
                    self.kill(who, hanged=Tribunal())
            case NightDeath(who):
                self.kill(who, last_night=True)
            case DayDeath(who):
                self.kill(who)
            case PutToDeath(who, guilty, innocent):
                self.kill(who, hanged=Vote(guilty, innocent))
            case Prosecuted(who):
                self.kill(who, hanged=Prosecution())
            case DayStart(1):
                self.judge_miscoloured_townies()
            case DayStart(n):
                self.time = DayTime(n, Time.DAY)
                for dc in self.dced:
                    self.kill(dc, last_night=True, dced=True)
                self.dced.clear()
            case NightStart(n):
                self.time = DayTime(n, Time.NIGHT)
                self.death_popped = False
                self.in_trib = False
            case LeftTown(who, _):
                them = self.players[who]
                if them.starting_ident.role == by_name("Cursed Soul"):
                    for player in self.players.values():
                        if player.starting_ident.role == by_name("Cursed Soul"):
                            player.won = True
                else:
                    them.won = True
            case HuntWarning(days_left):
                if not self.hunt_reached:
                    self.hunt_reached = self.time.in_days(days_left - 3)
            case TribunalDeclaration() | TribunalCount():
                self.in_trib = True
            case Disconnect(who):
                self.dced.add(who)
            case Reconnect(who):
                try:
                    self.dced.remove(who)
                except KeyError:
                    # shit. they're still alive
                    self.players[who].died = None
            case DeathPop():
                self.death_popped = True
            case DrawWarning():
                self.draw_tomorrow = True

    def result(self) -> GameResult:
        if len(self.players) < 5:
            raise NotLogError("input is not a gamelog")

        outcome = Outcome.NORMAL
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
                outcome = Outcome.DEATH
                victor = apocalypse if not self.pandora else coven
            elif self.time.in_days(-3) == self.hunt_reached:
                # hunt win
                outcome = Outcome.TT_COUNTDOWN
                victor = coven
            elif self.draw_tomorrow:
                # draw, no deaths
                outcome = Outcome.NO_DEATHS
                victor = None
            elif (
                in_game == {town, coven} and
                len(last_town := [x for x in self.players.values() if not x.died and x.ending_ident.faction == town]) == 1 and
                any([x.starting_ident.role == by_name("Cultist") for x in self.players.values()])
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
                    if hm.ending_ident.role == by_name("Hex Master"):
                        if not hm.died or any([not x.died and is_evil_raiser(x.ending_ident) for x in self.players.values()]):
                            outcome = Outcome.HEX_BOMB
                            victor = coven
                        break

        for player in self.players.values():
            if player.ending_ident.faction == victor:
                player.won = True

        return GameResult(tuple(self.players.values()), victor, self.hunt_reached, self.modifiers, self.vip, self.time, outcome)
