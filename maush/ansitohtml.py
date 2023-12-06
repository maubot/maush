# maushbot - A maubot to execute shell commands in maush from Matrix.
# Copyright (C) 2023 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>
import html

from stransi import Ansi, SetAttribute, SetColor
from stransi.attribute import Attribute
from stransi.color import ColorRole
from ochre import Color, RGB
from attr import dataclass


@dataclass
class ANSIHTML:
    fg: Color | None = None
    bg: Color | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    blink: bool = False
    reverse: bool = False
    hidden: bool = False

    @property
    def is_default(self) -> bool:
        return not any([self.fg, self.bg, self.bold, self.dim, self.italic, self.underline, self.strikethrough, self.blink, self.reverse, self.hidden])

    def update_attribute(self, attr: Attribute) -> None:
        if attr == Attribute.NORMAL:
            self.fg = None
            self.bg = None
            self.bold = False
            self.dim = False
            self.italic = False
            self.underline = False
            self.blink = False
            self.hidden = False
        elif attr == Attribute.BOLD:
            self.bold = True
            self.dim = False
        elif attr == Attribute.DIM:
            self.dim = True
            self.bold = False
        elif attr == Attribute.NEITHER_BOLD_NOR_DIM:
            self.bold = False
            self.dim = False
        elif attr == Attribute.ITALIC:
            self.italic = True
        elif attr == Attribute.NOT_ITALIC:
            self.italic = False
        elif attr == Attribute.UNDERLINE:
            self.underline = True
        elif attr == Attribute.NOT_UNDERLINE:
            self.underline = False
        elif attr == Attribute.STRIKETHROUGH:
            self.strikethrough = True
        elif attr == Attribute.NOT_STRIKETHROUGH:
            self.strikethrough = False
        elif attr == Attribute.BLINK:
            self.blink = True
        elif attr == Attribute.NOT_BLINK:
            self.blink = False
        elif attr == Attribute.REVERSE:
            self.reverse = True
        elif attr == Attribute.NOT_REVERSE:
            self.reverse = False
        elif attr == Attribute.HIDDEN:
            self.hidden = True
        elif attr == Attribute.NOT_HIDDEN:
            self.hidden = False

    @property
    def open_tags(self) -> str:
        tags = []
        if self.hidden:
            tags.append("<span data-mx-spoiler>")
        if self.fg or self.bg:
            fg, bg = self.fg, self.bg
            if self.reverse:
                if not fg:
                    fg = RGB(0, 0, 0)
                if not bg:
                    bg = RGB(255, 255, 255)
                fg, bg = bg, fg
            tags.append("<font")
            if fg:
                tags.append(f' color="{fg.hex}"')
            if bg:
                tags.append(f' data-mx-bg-color="{bg.hex}"')
            tags.append(">")
        if self.bold:
            tags.append("<strong>")
        if self.italic:
            tags.append("<em>")
        if self.strikethrough:
            tags.append("<del>")
        if self.underline:
            tags.append("<u>")
        return "".join(tags)

    @property
    def close_tags(self) -> str:
        tags = []
        if self.underline:
            tags.append("</u>")
        if self.strikethrough:
            tags.append("</del>")
        if self.italic:
            tags.append("</em>")
        if self.bold:
            tags.append("</strong>")
        if self.fg or self.bg:
            tags.append("</font>")
        if self.hidden:
            tags.append("</span data-mx-spoiler>")
        return "".join(tags)


def _ansi_to_html(text: str) -> str:
    output = []
    tags = ANSIHTML()
    for instruction in Ansi(text).instructions():
        if isinstance(instruction, str):
            if not tags.is_default:
                output.append(tags.open_tags)
            output.append(html.escape(instruction))
            if not tags.is_default:
                output.append(tags.close_tags)
        elif isinstance(instruction, SetColor):
            if instruction.role == ColorRole.FOREGROUND:
                tags.fg = instruction.color
            elif instruction.role == ColorRole.BACKGROUND:
                tags.bg = instruction.color
        elif isinstance(instruction, SetAttribute):
            tags.update_attribute(instruction.attribute)
    return "".join(output)


def ansi_to_html(text: str) -> str:
    try:
        return _ansi_to_html(text)
    except Exception:
        return html.escape(text)