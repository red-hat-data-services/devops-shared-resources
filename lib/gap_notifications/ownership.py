"""Live, anonymous component ownership policy. No routing or identity decisions."""
import re
import yaml
import requests
from lib.gap_component_map import normalize_component_name

BASE = "https://gitlab.cee.redhat.com/wznoinsk/rhoai-monitoring/-/raw/main/data/"
LIMIT = 1024 * 1024


def fetch_mapping(name, *, ca_bundle=None):
    session = requests.Session()
    session.trust_env = False
    try:
        with session.get(BASE + name, timeout=(5, 15), allow_redirects=False,
                         stream=True, verify=ca_bundle or True) as response:
            if response.status_code != 200:
                raise ValueError("ownership mapping unavailable")
            body = bytearray()
            for chunk in response.iter_content(8192):
                body.extend(chunk)
                if len(body) > LIMIT:
                    raise ValueError("ownership mapping too large")
            return bytes(body)
    finally:
        session.close()


def _names(value):
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or any(
            not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values):
        raise ValueError("invalid Jira ownership")
    return list(dict.fromkeys(values))


class OwnershipPolicy:
    def __init__(self, jira, teams):
        if not isinstance(jira, dict) or not isinstance(teams, dict):
            raise ValueError("invalid ownership mapping")
        mappings = jira.get('mappings')
        if not isinstance(mappings, dict):
            raise ValueError("missing Jira mappings")
        if any(not isinstance(k, str) or not k.strip() for k in mappings):
            raise ValueError("invalid Jira ownership key")
        self.jira = {k: _names(v) for k, v in mappings.items()}
        self.default_jira = _names(jira.get('_default', 'DevOps'))
        components = teams.get('components')
        if not isinstance(components, dict) or not isinstance(components.get('default'), dict):
            raise ValueError("missing Slack default")
        default_handle = components['default'].get('slack_team_handle')
        if (not isinstance(default_handle, str) or default_handle == 'unknown'
                or default_handle.lower() in {'here', 'channel', 'everyone'}
                or not re.fullmatch(r'[a-zA-Z0-9_-]+', default_handle)):
            raise ValueError("invalid Slack default handle")
        self.teams = {}
        for name, value in components.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
                raise ValueError("invalid Slack ownership")
            handle = value.get('slack_team_handle', default_handle)
            if handle == 'unknown':
                handle = default_handle
            if (not isinstance(handle, str) or handle.lower() in {'here', 'channel', 'everyone'}
                    or not re.fullmatch(r'[a-zA-Z0-9_-]+', handle)):
                raise ValueError("invalid Slack handle")
            self.teams[name] = handle

    @classmethod
    def load(cls, fetch=None, *, ca_bundle=None):
        fetch = fetch or (lambda name: fetch_mapping(name, ca_bundle=ca_bundle))
        return cls(yaml.safe_load(fetch('konflux-to-jira-component-map.yaml')),
                   yaml.safe_load(fetch('rhoai-component-data.yaml')))

    @staticmethod
    def _lookup(mapping, component, default):
        if not isinstance(component, str):
            return default
        return mapping.get(component, mapping.get(normalize_component_name(component), default))

    def extra_fields(self, observation):
        names = self._lookup(self.jira, observation.get('component'), self.default_jira)
        return {'components': [{'name': name} for name in names]}

    def blocks(self, observation, text, *, root=False):
        """Only the trusted mapped handle gets automatic parsing."""
        blocks = []
        if root and observation.get("status") == "failure":
            handle = self._lookup(self.teams, observation.get("component"), self.teams["default"])
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn", "text": "@" + handle, "verbatim": False}})
        blocks.extend({"type": "section", "text": {
            "type": "mrkdwn", "text": text[i:i + 3000], "verbatim": True}}
            for i in range(0, len(text), 3000))
        return blocks
