typedef signed char int8_t; typedef unsigned char uint8_t;
typedef short int16_t; typedef unsigned short uint16_t;
typedef int int32_t; typedef unsigned int uint32_t;
typedef long long int64_t; typedef unsigned long long uint64_t;
#define NULL ((void*)0)
#define INT8_MIN (-128)
#define INT8_MAX 127
#define UINT8_MAX 255
#define INT16_MIN (-32768)
#define INT16_MAX 32767
#define UINT16_MAX 65535
#define INT32_MIN (-2147483647-1)
#define INT32_MAX 2147483647
#define UINT32_MAX 4294967295U
#define INT64_MIN (-9223372036854775807LL-1)
#define INT64_MAX 9223372036854775807LL
#define UINT64_MAX 18446744073709551615ULL
int printf(const char *, ...);
int strcmp(const char *, const char *);
extern uint32_t crc32_context;
void crc32_gentab(void);
void transparent_crc(uint64_t val, char* vname, int flag);
void transparent_crc_bytes(char *ptr, int nbytes, char* vname, int flag);
void platform_main_begin(void);
void platform_main_end(uint32_t crc, int flag);
