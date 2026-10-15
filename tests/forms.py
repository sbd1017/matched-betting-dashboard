"""Submit forms the way a browser does: only what the rendered page contains (no token added by the test)."""
from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser


@dataclass
class Form:
    method: str
    action: str
    fields: dict = field(default_factory=dict)       # name -> default value the browser would send
    names: list = field(default_factory=list)        # every named control (incl. text inputs left blank)


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self.form: Form | None = None
        self.select: str | None = None
        self.select_first: str | None = None
        self.textarea: str | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            self.form = Form(a.get("method", "get").lower(), unescape(a.get("action", "")))
            self.forms.append(self.form)
        if self.form is None:
            return
        name = a.get("name")
        if tag == "input" and name:
            kind = a.get("type", "text").lower()
            self.form.names.append(name)
            if kind in ("checkbox", "radio"):
                if "checked" in a:
                    self.form.fields[name] = a.get("value", "on")
            elif kind not in ("submit", "button", "file"):
                self.form.fields[name] = a.get("value", "")
        elif tag == "select" and name:
            self.select, self.select_first = name, None
            self.form.names.append(name)
            self.form.fields.setdefault(name, "")
        elif tag == "option" and self.select:
            value = a.get("value", "")
            if self.select_first is None:
                self.select_first = value
                self.form.fields[self.select] = value
            if "selected" in a:
                self.form.fields[self.select] = value
        elif tag == "textarea" and name:
            self.textarea = name
            self.form.names.append(name)
            self.form.fields[name] = ""
        elif tag == "button" and name:
            self.form.names.append(name)

    def handle_data(self, data):
        if self.form is not None and self.textarea:
            self.form.fields[self.textarea] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.form = None
        elif tag == "select":
            self.select = None
        elif tag == "textarea":
            self.textarea = None


def forms_in(html: str) -> list[Form]:
    p = _Parser()
    p.feed(html)
    return p.forms


def post_forms(html: str) -> list[Form]:
    return [f for f in forms_in(html) if f.method == "post"]


def find_form(html: str, action: str, has: str | None = None) -> Form:
    matches = [f for f in post_forms(html) if f.action == action and (has is None or has in f.names)]
    assert matches, f"no POST form for {action} (has {has}) on the page: {[f.action for f in post_forms(html)]}"
    return matches[0]


def submit(client, page: str, action: str, values: dict | None = None, has: str | None = None):
    """GET `page`, fill the form posting to `action` like a person would, and send it."""
    html = client.get(page).text
    form = find_form(html, action, has)
    data = {**form.fields, **{k: str(v) for k, v in (values or {}).items()}}
    for k in values or {}:
        assert k in form.names, f"{action} form has no field {k!r}: {form.names}"
    return client.post(action, data=data, headers={"Origin": "http://testserver"}, follow_redirects=False)
