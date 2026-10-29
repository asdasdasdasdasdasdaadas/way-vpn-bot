"""Build a source archive from an explicit allowlist; never include secrets/data."""
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parent.parent
files = ['Dockerfile', 'requirements.txt', 'compose.production.yaml', '.dockerignore',
         '.env.production.example', 'README.md']
for folder, pattern in [('vpn_bot', '*.py'), ('tests', '*.py'), ('deploy', '*'), ('scripts', '*')]:
    files.extend(str(path.relative_to(root)) for path in (root / folder).glob(pattern) if path.is_file())
output = root / 'dist' / 'vpn-bot-deploy.tar.gz'
output.parent.mkdir(exist_ok=True)
with tarfile.open(output, 'w:gz') as archive:
    for name in sorted(files):
        archive.add(root / name, arcname=name, recursive=False)
with tarfile.open(output) as archive:
    assert set(archive.getnames()) == set(files)
    for member in archive.getmembers():
        assert member.isfile() and not member.issym()
print(f'Created {output.name}: {len(files)} files, {output.stat().st_size} bytes')
