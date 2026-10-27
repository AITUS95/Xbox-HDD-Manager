/* Mutations are restricted by the runner to its disposable copy. */
#include <fatx.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
static int checks;
#define CHECK(x) do { if (!(x)) {fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#x); exit(1);} checks++; } while (0)
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    const char *letters="cexyz";
    int write_phase = strcmp(argv[2], "write") == 0;
    for (int p=0;p<5;p++) {
        uint64_t offset,size;
        struct fatx_fs fs={0};
        struct fatx_attr attr;
        CHECK(fatx_drive_to_offset_size(letters[p],&offset,&size)==0);
        CHECK(fatx_open_device(&fs,argv[1],offset,size,512,0)==0);
        unsigned char data[131113], out[131113];
        for(size_t i=0;i<sizeof(data);i++) data[i]=(i*13+p)%253;
        if(write_phase) {
            CHECK(fatx_mkdir(&fs,"/PersistenceTest")==0);
            CHECK(fatx_mknod(&fs,"/PersistenceTest/keep.bin")==0);
            CHECK(fatx_write(&fs,"/PersistenceTest/keep.bin",0,sizeof(data),data)==sizeof(data));
            /* Edit across cluster boundaries, append, then truncate the final result. */
            CHECK(fatx_write(&fs,"/PersistenceTest/keep.bin",16380,32,"0123456789abcdefghijklmnopqrstuv")==32);
            CHECK(fatx_write(&fs,"/PersistenceTest/keep.bin",sizeof(data),4,"TAIL")==4);
            CHECK(fatx_truncate(&fs,"/PersistenceTest/keep.bin",100003)==0);
            CHECK(fatx_mknod(&fs,"/PersistenceTest/delete.bin")==0);
            CHECK(fatx_write(&fs,"/PersistenceTest/delete.bin",0,4,"gone")==4);
            CHECK(fatx_unlink(&fs,"/PersistenceTest/delete.bin")==0);
            CHECK(fatx_mknod(&fs,"/PersistenceTest/oldname.txt")==0);
            CHECK(fatx_write(&fs,"/PersistenceTest/oldname.txt",0,7,"renamed")==7);
            CHECK(fatx_rename(&fs,"/PersistenceTest/oldname.txt","/PersistenceTest/newname.txt",false,false)==0);
        } else {
            memcpy(data+16380,"0123456789abcdefghijklmnopqrstuv",32);
            CHECK(fatx_get_attr(&fs,"/PersistenceTest/keep.bin",&attr)==0);
            CHECK(attr.file_size==100003);
            memset(out,0,sizeof(out));
            CHECK(fatx_read(&fs,"/PersistenceTest/keep.bin",0,sizeof(out),out)==100003);
            CHECK(memcmp(data,out,100003)==0);
            CHECK(fatx_get_attr(&fs,"/PersistenceTest/delete.bin",&attr)==FATX_STATUS_FILE_NOT_FOUND);
            CHECK(fatx_get_attr(&fs,"/PersistenceTest/oldname.txt",&attr)==FATX_STATUS_FILE_NOT_FOUND);
            CHECK(fatx_read(&fs,"/PersistenceTest/newname.txt",0,sizeof(out),out)==7);
            CHECK(memcmp(out,"renamed",7)==0);
        }
        CHECK(fatx_close_device(&fs)==0);
        printf("%c: %s persisted edits/deletion/rename/truncate OK\n",letters[p],argv[2]);
    }
    printf("PASS %s: %d checks\n",argv[2],checks);
    return 0;
}
