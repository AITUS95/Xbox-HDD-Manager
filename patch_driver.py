#!/usr/bin/python3
"""Small auditable patches to the pinned XboxDev driver. Fail on unexpected source."""
from pathlib import Path
import sys


def patch(source):
    path = Path(source) / 'fatxfs/fatxfs_fuse.c'
    text = path.read_text()
    if 'xbox_hdd_sync' in text:
        return
    replacements = {
        '#include <errno.h>': '#include <errno.h>\n#include <unistd.h>',
        'pd.fs = malloc(sizeof(struct fatx_fs));': 'pd.fs = calloc(1, sizeof(struct fatx_fs));',
        'void fatx_fuse_destroy(void *data);': '''void fatx_fuse_destroy(void *data);

/* libfatx uses a stdio buffer and a separate FAT cache. Both must reach NBD. */
extern int fatx_flush_fat_cache(struct fatx_fs *fs);
static int xbox_hdd_sync(const char *path, int datasync, struct fuse_file_info *fi)
{
    struct fuse_context *context = fuse_get_context();
    struct fatx_fuse_private_data *pd = context->private_data;
    (void)path; (void)datasync; (void)fi;
    if (fatx_flush_fat_cache(pd->fs) || fflush(pd->fs->device)) return -EIO;
    return fsync(fileno(pd->fs->device)) == 0 ? 0 : -errno;
}
static int xbox_hdd_flush(const char *path, struct fuse_file_info *fi)
{
    return xbox_hdd_sync(path, 0, fi);
}''',
        '    .destroy  = fatx_fuse_destroy,': '''    .flush    = xbox_hdd_flush,
    .fsync    = xbox_hdd_sync,
    .destroy  = fatx_fuse_destroy,''',
        '    fatx_close_device(pd->fs);': '''    if (xbox_hdd_sync("/", 0, NULL))
        fprintf(stderr, "Xbox HDD: final synchronization failed\\n");
    fatx_close_device(pd->fs);''',
    }
    for old, new in replacements.items():
        if text.count(old) != 1:
            raise RuntimeError(f'Unexpected driver source: {old}')
        text = text.replace(old, new)
    path.write_text(text)


if __name__ == '__main__':
    patch(sys.argv[1])
