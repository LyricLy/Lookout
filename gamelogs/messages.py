import re
import sre_parse as sp
from collections import defaultdict
from dataclasses import dataclass
from typing import Self, ClassVar, Final
from lxml.etree import tostring
from lxml.html import HtmlElement
from lxml.cssselect import CSSSelector


class NotMessage(Exception):
    pass

def implore[T](x: T | None) -> T:
    if not x:
        raise NotMessage(f"{x} is not")
    return x

def get_text(x: HtmlElement) -> str:
    # what the hell?
    return tostring(x, with_tail=False, method="text", encoding="unicode")

@dataclass
class Line:
    contents: list[HtmlElement]

    def __getitem__(self, index: int) -> HtmlElement:
        try:
            return self.contents[index]
        except IndexError:
            raise NotMessage(f"line does not have index {index}")

    def __str__(self):
        return " ".join(map(str, self.contents))


class Message:
    @classmethod
    def from_line(cls, line: Line) -> Message:
        try:
            return Chat.from_line(line)
        except NotMessage:
            pass

        try:
            return SystemMessage.from_line(line)
        except NotMessage:
            pass

        try:
            return DeadChat.from_line(line)
        except NotMessage:
            pass

        try:
            return PlayerInfo.from_line(line)
        except NotMessage:
            pass

        raise NotMessage()


LAST_N = 7
def last_n(pattern):
    s = ""
    for rtype, args in sp.parse(pattern.pattern)[-LAST_N:]:  # type: ignore
        if rtype != sp.LITERAL:
            return None
        s += chr(args)  # type: ignore
    return s

class SystemMessage(Message):
    regex: ClassVar[re.Pattern]
    leaves: Final[defaultdict[str | None, list[type[Self]]]] = defaultdict(list)

    def __init_subclass__(cls):
        SystemMessage.leaves[last_n(cls.regex)].append(cls)

    @classmethod
    def from_match(cls, m: re.Match) -> Self:
        return cls(*[None if x is None else int(x) if x.isdigit() else x for x in m.groups()])

    @classmethod
    def from_line(cls, line: Line) -> Self:
        first_text = get_text(line[0])
        last = first_text[-LAST_N:]
        for subcls in cls.leaves.get(last) or cls.leaves[None]:
            if m := subcls.regex.fullmatch(first_text):
                return subcls.from_match(m)
        raise NotMessage("line is not a system message")


@dataclass
class Chat(Message):
    who_number: int
    who_name: str
    content: HtmlElement

    @classmethod
    def from_line(cls, line: Line) -> Self:
        implore(not line[2].get("style"))
        number = line[0].text
        implore(number[-1] == "]")
        name = line[1].text
        return cls(
            who_number=int(number[1:-1]),
            who_name=name,
            content=line[2],
        )

@dataclass
class DeadChat(Message):
    who_number: int
    who_name: str
    content: HtmlElement

    @classmethod
    def from_line(cls, line: Line) -> Self:
        implore(line[2].get("style") == "color:#689194")
        number = get_text(line[0])
        name = line[1].text.replace("-", " ")
        return cls(
            who_number=int(number[1:-1]),
            who_name=name,
            content=line[2],
        )

@dataclass
class PlayerInfo(Message):
    number: int
    game_name: str
    account_name: str
    role: tuple[str, str]
    prev_role: tuple[str, str] | None
    last_will: HtmlElement | None
    is_vip: bool

    tooltiptext_select = CSSSelector(".tooltipprev .tooltiptext span", translator="html")
    will_select = CSSSelector(".tooltipwill .tooltiptext", translator="html")

    @staticmethod
    def parse_role(tag: HtmlElement) -> tuple[str, str]:
        colour = re.fullmatch(r"color:#([0-9A-F]{6})", str(tag.get("style")))
        return tag.text.strip(), colour[1] if colour else "000000"

    @classmethod
    def from_line(cls, line: Line) -> Self:
        header = implore(line[0].text).split("] ")
        implore(len(header) > 1)

        username = get_text(line[-2])[11:-1]
        role = cls.parse_role(line[1])
        prev_role = cls.parse_role(prev[0]) if (prev := cls.tooltiptext_select(line[2])) else None
        last_will = will[0] if (will := cls.will_select(line[-3])) else None
        is_vip = line[2].text == " ★ "
        return cls(
            number=int(header[0][1:]),
            game_name=header[1][:-3],
            account_name=username,
            role=role,
            prev_role=prev_role,
            last_will=last_will,
            is_vip=is_vip,
        )

@dataclass
class VoteAgainstInstead(SystemMessage):
    who: str
    against: str
    regex = re.compile(r"(.+) instead voted against (.+)\.")

@dataclass
class VoteAgainst(SystemMessage):
    who: str
    against: str
    regex = re.compile(r"(.+) voted against (.+)\.")

@dataclass
class CancelVote(SystemMessage):
    who: str
    regex = re.compile(r"(.+) cancelled their vote\.")

@dataclass
class VoteToExecute(SystemMessage):
    who: str
    against: str
    regex = re.compile(r"(.+) voted to execute (.+)\.")

@dataclass
class VoteGuilty(SystemMessage):
    who: str
    against: str
    regex = re.compile(r"(.+) voted (.+) guilty!")

@dataclass
class Abstain(SystemMessage):
    who: str
    against: str
    regex = re.compile(r"(.+) abstained on (.+)!")

@dataclass
class VoteInnocent(SystemMessage):
    who: str
    against: str
    regex = re.compile(r"(.+) voted (.+) innocent!")

@dataclass
class PovDied(SystemMessage):
    regex = re.compile(r"You were (?:killed|executed|murdered|set on fire|haunted).*\!|You have succumbed to a pestilence\!|You jailed War and were obliterated\. Flummery\!")

@dataclass
class Whispering(SystemMessage):
    who: str
    to: str
    regex = re.compile(r"(.+) is whispering to (.+)\.")

@dataclass
class LeftAWill(SystemMessage):
    who: str
    regex = re.compile(r"(.+) left a last will\.")

@dataclass
class Upped(SystemMessage):
    who: str
    regex = re.compile(r"(.+) was voted up to trial\.")

@dataclass
class DayStart(SystemMessage):
    day: int
    regex = re.compile(r"Day (\d+)")

@dataclass
class NightStart(SystemMessage):
    night: int
    regex = re.compile(r"Night (\d+)")

@dataclass
class DayDeath(SystemMessage):
    who: str
    regex = re.compile(r"(.+) died today\.")

@dataclass
class NightDeath(SystemMessage):
    who: str
    regex = re.compile(r"(.+) died last night\.")

@dataclass
class TribunalDeclaration(SystemMessage):
    who: str | None
    regex = re.compile(r"(?:(.+) the Marshal,|A Marshal) has declared a Tribunal\.")

@dataclass
class TribunalCount(SystemMessage):
    num: int
    regex = re.compile(r"You may execute (\d+) (?:people|person) today\.")

@dataclass
class MayorReveal(SystemMessage):
    who: str
    regex = re.compile(r"(.+) has revealed themself as the Mayor!")

@dataclass
class PutToDeath(SystemMessage):
    who: str
    guilty: int
    innocent: int
    regex = re.compile(r"The Town decided to put (.+) to death by a vote of (\d+) to (\d+)\.")

@dataclass
class Pardoned(SystemMessage):
    who: str
    innocent: int
    guilty: int
    regex = re.compile(r"The Town decided to pardon (.+) by a vote of (\d+) to (\d+)\.")

@dataclass
class Prosecuted(SystemMessage):
    who: str
    regex = re.compile(r"(.+) has been judged guilty and will be put to death\.")

@dataclass
class TrialsRemaining(SystemMessage):
    count: int
    regex = re.compile(r"There are (\d) possible trials remaining today\.")

@dataclass
class Disconnect(SystemMessage):
    who: str
    regex = re.compile(r"(.+) has disconnected from life\.")

@dataclass
class Reconnect(SystemMessage):
    who: str
    regex = re.compile(r"(.+) has reconnected to life\.")

@dataclass
class LeftTown(SystemMessage):
    who: str
    role: str
    regex = re.compile(r"(.+) has accomplished their goal as (.+) and left town\.")

@dataclass
class DeathPop(SystemMessage):
    regex = re.compile("Now Soul Collector has become Death, Destroyer of Worlds and Horseman of the Apocalypse!")

@dataclass
class HuntWarning(SystemMessage):
    days_left: int
    regex = re.compile(r"There are (\d) days left to find the Town Traitor\.")

@dataclass
class DrawWarning(SystemMessage):
    regex = re.compile(r"If no one dies by tomorrow the game will end in a draw\.")

@dataclass
class StartJunk(SystemMessage):
    regex = re.compile(r"PLAYER INFO")
