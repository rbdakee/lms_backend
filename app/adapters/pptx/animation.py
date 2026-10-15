"""Анимации слайда — встроенные в PowerPoint (`p:timing`) и переход (`p:transition`).

Никакого скрипта и видео: это та самая разметка, которую PowerPoint пишет сам,
когда в «Анимации» выбирают «Появление» или «Выцветание» и «Начало: по щелчку».
Файл открывается без ремонта в PowerPoint, Keynote и Google Slides; редактор,
который анимацию не знает, показывает слайд целиком — ничего не пропадает.

Устройство (ECMA-376, часть 1, 19.5): корень `tmRoot` → последовательность
`mainSeq` → по одному «щелчку» на `p:par` с условием `delay="indefinite"`
(ждать клика) → вложенный `p:par` → эффекты. Первый эффект щелчка —
`clickEffect`, остальные в том же щелчке — `withEffect` (так у шага две фигуры:
плашка и кружок с номером). Каждый эффект — `p:set` видимости, у «выцветания»
ещё и `p:animEffect` с фильтром `fade`. Фигура с текстом, которую открывают
по абзацам или целиком, обязана быть и в `p:bldLst`, иначе PowerPoint при
открытии предложит восстановить файл. `grpId` эффекта и `grpId` записи
в `bldLst` — одно число.
"""

from dataclasses import dataclass

from lxml import etree
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls

# Эффекты входа из окна «Анимация»: 1 — «Появление», 10 — «Выцветание»
PRESET = {"appear": 1, "fade": 10}
FADE_MS = 500


@dataclass(frozen=True)
class Target:
    """Что открывается: фигура целиком или один её абзац (счёт с нуля)."""

    shape_id: int
    para: int | None = None


def animate(slide, groups: list[list[Target]], kind: str) -> None:
    """Расставить щелчки: каждая группа — один щелчок, цели группы
    открываются вместе. `kind` — `appear` или `fade`; `none` не зовёт сюда."""
    if not groups or kind not in PRESET:
        return
    ids = iter(range(3, 10_000))
    clicks = []
    for group in groups:
        # Номера узлов идут в порядке документа: щелчок, его обёртка, эффекты
        outer, inner = next(ids), next(ids)
        effects = [
            _effect(ids, target, kind, "clickEffect" if number == 0 else "withEffect")
            for number, target in enumerate(group)
        ]
        clicks.append(
            f'<p:par><p:cTn id="{outer}" fill="hold">'
            '<p:stCondLst><p:cond delay="indefinite"/></p:stCondLst><p:childTnLst>'
            f'<p:par><p:cTn id="{inner}" fill="hold">'
            '<p:stCondLst><p:cond delay="0"/></p:stCondLst>'
            f"<p:childTnLst>{''.join(effects)}</p:childTnLst></p:cTn></p:par>"
            "</p:childTnLst></p:cTn></p:par>"
        )
    xml = (
        f"<p:timing {nsdecls('p')}><p:tnLst><p:par>"
        '<p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
        '<p:seq concurrent="1" nextAc="seek">'
        '<p:cTn id="2" dur="indefinite" nodeType="mainSeq">'
        f"<p:childTnLst>{''.join(clicks)}</p:childTnLst></p:cTn>"
        '<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond>'
        "</p:prevCondLst>"
        '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond>'
        "</p:nextCondLst></p:seq></p:childTnLst></p:cTn></p:par></p:tnLst>"
        f"<p:bldLst>{_build_list(groups)}</p:bldLst></p:timing>"
    )
    _after_transition(slide, parse_xml(xml))


def fade_transition(slide) -> None:
    """Плавная смена слайда. Стоит перед `p:timing`: порядок детей `p:sld`
    в схеме — cSld, clrMapOvr, transition, timing."""
    _after_clr_map(
        slide, parse_xml(f'<p:transition {nsdecls("p")} spd="med"><p:fade/></p:transition>')
    )


def _effect(ids, target: Target, kind: str, node: str) -> str:
    effect_id, set_id = next(ids), next(ids)
    spid = target.shape_id
    if target.para is None:
        shape = f'<p:spTgt spid="{spid}"/>'
    else:
        shape = (
            f'<p:spTgt spid="{spid}"><p:txEl><p:pRg st="{target.para}" end="{target.para}"/>'
            "</p:txEl></p:spTgt>"
        )
    visible = (
        "<p:set><p:cBhvr>"
        f'<p:cTn id="{set_id}" dur="1" fill="hold">'
        '<p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>'
        f"<p:tgtEl>{shape}</p:tgtEl>"
        "<p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst></p:cBhvr>"
        '<p:to><p:strVal val="visible"/></p:to></p:set>'
    )
    fade = ""
    if kind == "fade":
        fade = (
            '<p:animEffect transition="in" filter="fade"><p:cBhvr>'
            f'<p:cTn id="{next(ids)}" dur="{FADE_MS}"/>'
            f"<p:tgtEl>{shape}</p:tgtEl></p:cBhvr></p:animEffect>"
        )
    return (
        f'<p:par><p:cTn id="{effect_id}" presetID="{PRESET[kind]}" presetClass="entr" '
        f'presetSubtype="0" fill="hold" grpId="0" nodeType="{node}">'
        f'<p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>{visible}{fade}'
        "</p:childTnLst></p:cTn></p:par>"
    )


def _build_list(groups: list[list[Target]]) -> str:
    """Запись на каждую анимируемую фигуру с текстом — один раз: по абзацам
    (`build="p"`) или целиком (`animBg="1"`)."""
    by_paragraph: dict[int, bool] = {}
    for target in (t for group in groups for t in group):
        by_paragraph[target.shape_id] = by_paragraph.get(target.shape_id, False) or (
            target.para is not None
        )
    return "".join(
        f'<p:bldP spid="{spid}" grpId="0" build="p"/>'
        if paragraphs
        else f'<p:bldP spid="{spid}" grpId="0" animBg="1"/>'
        for spid, paragraphs in by_paragraph.items()
    )


def _after_clr_map(slide, node) -> None:
    root = slide._element
    anchor = root.find(f"{{{root.nsmap['p']}}}clrMapOvr")
    if anchor is None:
        anchor = root.find(f"{{{root.nsmap['p']}}}cSld")
    anchor.addnext(node)


def _after_transition(slide, node) -> None:
    root = slide._element
    ns = root.nsmap["p"]
    anchor = root.find(f"{{{ns}}}transition")
    if anchor is None:
        anchor = root.find(f"{{{ns}}}clrMapOvr")
    if anchor is None:
        anchor = root.find(f"{{{ns}}}cSld")
    anchor.addnext(node)


def pretty(slide) -> str:
    """Разметка слайда для отладки и тестов."""
    return etree.tostring(slide._element, pretty_print=True, encoding="unicode")
