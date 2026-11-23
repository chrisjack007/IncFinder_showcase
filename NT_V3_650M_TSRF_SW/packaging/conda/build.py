"""Install into the Conda build prefix without publishing local source paths."""
import csv
from importlib.metadata import version
from pathlib import Path
import subprocess
import sys
import sysconfig


def main():
    subprocess.run([sys.executable, '-m', 'pip', 'install', '.', '--no-deps',
                    '--no-build-isolation'], check=True)
    dist_info = Path(sysconfig.get_path('purelib')) / (
        'incfinder_ntv3_tsrf-' + version('incfinder-ntv3-tsrf') + '.dist-info')
    if not dist_info.resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError('Distribution metadata must remain inside the build environment')
    origin = dist_info / 'direct_url.json'
    if origin.is_file():
        record = dist_info / 'RECORD'
        with record.open(encoding='utf-8', newline='') as stream:
            rows = list(csv.reader(stream))
        member = dist_info.name + '/direct_url.json'
        retained = [row for row in rows if row[0] != member]
        if len(rows) - len(retained) != 1:
            raise RuntimeError('Expected one local installation origin in RECORD')
        origin.unlink()
        with record.open('w', encoding='utf-8', newline='') as stream:
            csv.writer(stream, lineterminator='\n').writerows(retained)


if __name__ == '__main__':
    main()
