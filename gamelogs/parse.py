from typing import Iterator, Any, Generator
from lxml import html
from lxml.etree import ParserError

from .messages import *
from .model import *
from .errors import *
from .analyzer import *


def clean_tos2_tags(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"<style>(.*?)</style>", r"<goodstyle>\1</goodstyle>", text, flags=re.DOTALL)
    text = re.sub(r"</?(?:font|link|sprite|color|style)[^>]*>", "", text)
    text = text.replace("goodstyle>", "style>")
    # bad formatting bug in gamelogs
    return re.sub(r'<div class="tooltipprev">(.+?)</span></div>$', r'<div class="tooltipprev">\1</div></span>', text, flags=re.MULTILINE)

def to_lines(text: str) -> Iterator[Line]:
    try:
        soup = html.document_fromstring(text)
    except ParserError:
        raise InvalidHTMLError("data is not valid HTML")
    body = soup.find("body")
    if body is None:
        return
    line = []
    for child in body:
        if len(child) and child[0].tag == "br":
            child[0].drop_tree()
            yield Line(line)
            line = [child]
        else:
            line.append(child)
    yield Line(line)

def to_messages(text: str, clean_tags: bool) -> Iterator[Message]:
    for line in to_lines(clean_tos2_tags(text) if clean_tags else text):
        try:
            message = Message.from_line(line)
        except NotMessage:
            pass
        else:
            yield message

def parse[R](text: str, analyzer: Analyzer[Any, R], *, clean_tags: bool = True) -> R:
    for message in to_messages(text, clean_tags):
        analyzer.get_message(message)
    return analyzer.result()

def parse_result(text: str, *, clean_tags: bool = True) -> GameResult:
    return parse(text, ResultAnalyzer(), clean_tags=clean_tags)

def parse_iter[Y, R](text: str, analyzer: Analyzer[Y, R], *, clean_tags: bool = True) -> Generator[Y, None, R]:
    for message in to_messages(text, clean_tags):
        yield analyzer.get_message(message)
    return analyzer.result()
