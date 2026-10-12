"""Personal loaders keep unique runtime names while preserving original package files."""
import asyncio
from pathlib import Path
import frontmatter
from agentscope.skill import LocalSkillLoader, Skill
from app.config import settings


class PersonalSkillLoader(LocalSkillLoader):
    def __init__(self, directory, identity):
        super().__init__(directory=directory, scan_subdir=False)
        self.identity = identity

    async def list_skills(self):
        path = Path(self.directory) / 'SKILL.md'
        raw = await asyncio.to_thread(path.read_text, encoding='utf-8')
        parsed = frontmatter.loads(raw)
        sandbox = settings.WORKSPACE_BACKEND == 'docker'
        directory = self.identity['sandbox_path'] if sandbox else self.directory
        return [Skill(name=self.identity['name'], description=str(parsed.get('description') or self.identity['title']),
            dir=directory, markdown='技能文件目录：'+directory+'\n\n'+parsed.content,
            updated_at=path.stat().st_mtime)]


def skill_loader(directory, agent_model):
    identity = (agent_model.config or {}).get('_hub_runtime_loaders', {}).get(directory)
    return PersonalSkillLoader(directory, identity) if identity else LocalSkillLoader(directory=directory, scan_subdir=False)


def seed_paths(paths, agent_model):
    config = agent_model.config or {}
    personal = config.get('_hub_runtime_loaders', {})
    return sorted(set(path for path in paths if path not in personal) | set(config.get('_hub_runtime_seed_paths', [])))
