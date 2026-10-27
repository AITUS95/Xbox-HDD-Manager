/* Run ONLY against an expendable raw COPY. Tests libfatx without requiring FUSE. */
#include <fatx.h>
#include <stdio.h>
#include <unistd.h>
#include <stdint.h>
#include <stdlib.h>
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#x); exit(1); } tests++; } while(0)
static int tests;
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    const char *letters = "cexyz";
    for (int part=0; part<5; part++) {
        uint64_t off, size;
        struct fatx_fs fs = {0};
        CHECK(fatx_drive_to_offset_size(letters[part], &off, &size) == 0);
        CHECK(fatx_open_device(&fs, argv[1], off, size, 512, 0) == 0);
        struct fatx_dir dir;
        struct fatx_dirent entry, *next;
        struct fatx_attr attr;
        CHECK(fatx_open_dir(&fs, "/", &dir) == 0);
        int listed=0, status;
        while ((status = fatx_read_dir(&fs, &dir, &entry, &attr, &next)) != FATX_STATUS_END_OF_DIR) {
            CHECK(status == 0 || status == FATX_STATUS_FILE_DELETED);
            if (status == 0) listed++;
            CHECK(fatx_next_dir_entry(&fs, &dir) == 0);
            if (listed > 100000) return 3;
        }
        CHECK(fatx_close_dir(&fs, &dir) == 0);
        CHECK(fatx_mkdir(&fs, "/DriverTest") == 0);
        CHECK(fatx_mknod(&fs, "/DriverTest/test.bin") == 0);
        size_t length = 96*1024 + 37;
        unsigned char *data = malloc(length), *out = calloc(length,1);
        CHECK(data && out);
        for (size_t i=0; i<length; i++) data[i]=(i*37+part)%251;
        CHECK(fatx_write(&fs, "/DriverTest/test.bin", 0, length, data) == length);
        CHECK(fatx_read(&fs, "/DriverTest/test.bin", 0, length, out) == length);
        CHECK(memcmp(data, out, length) == 0);
        CHECK(fatx_close_device(&fs) == 0);
        memset(&fs,0,sizeof(fs));
        CHECK(fatx_open_device(&fs, argv[1], off, size, 512, 0) == 0);
        memset(out,0,length);
        CHECK(fatx_read(&fs, "/DriverTest/test.bin", 0, length, out) == length);
        CHECK(memcmp(data, out, length) == 0);
        CHECK(fatx_rename(&fs,"/DriverTest/test.bin","/DriverTest/renamed.bin",false,false) == 0);
        CHECK(fatx_get_attr(&fs,"/DriverTest/test.bin",&attr) == FATX_STATUS_FILE_NOT_FOUND);
        CHECK(fatx_truncate(&fs,"/DriverTest/renamed.bin",123) == 0);
        CHECK(fatx_read(&fs,"/DriverTest/renamed.bin",0,length,out) == 123);
        CHECK(memcmp(data,out,123) == 0);
        CHECK(fatx_unlink(&fs,"/DriverTest/renamed.bin") == 0);
        CHECK(fatx_rmdir(&fs,"/DriverTest") == 0);
        CHECK(fatx_close_device(&fs) == 0);
        memset(&fs,0,sizeof(fs));
        CHECK(fatx_open_device(&fs, argv[1], off, size, 512, 0) == 0);
        CHECK(fatx_get_attr(&fs,"/DriverTest",&attr) == FATX_STATUS_FILE_NOT_FOUND);
        CHECK(fatx_close_device(&fs) == 0);
        free(data); free(out);
        printf("%c: directory entries=%d, read/write/reopen/rename/truncate/delete OK\n",letters[part],listed);
    }
    printf("PASS: %d checks on raw copy\n",tests);
    return 0;
}
