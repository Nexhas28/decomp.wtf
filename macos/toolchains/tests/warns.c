typedef unsigned char u8; typedef signed char s8; typedef unsigned short u16;
struct S { int a; char b; } s1, *ps; union U { int i; float f; } u1;
extern int undeclared_ptr_user();
int w1(void) { u8 c = 300; s8 d = 200; u16 e = 70000; return c + d + e; }
int w2(u8 x) { if (x < 0) return 1; if (x > 255) return 2; if (x == -1) return 3; return 0; }
int w3(int *p, char *q) { return p == q; }
int w4(int x) { int y; if (x) y = 1; return y; }
int w5(void) { int *p = 12345; char *q = p; return (int)q + (long)&s1; }
int w6(void) { return 0x123456789; }
int w7(int x) { return x << 40; }
int w8(void) { return 1 / 0 + 5 % 0; }
int w9(int a) { return a = 5; }
void w10(void) { "unused"; 1 + 2; }
int w11(void) { return undeclared_ptr_user(1, 2.0, "s"); }
int w12(int x) { switch (x) { case 1: case 1+0*2: return 2; } return 0; }
int w13(void) { char buf[2] = "toolong"; return buf[0]; }
int w14() { return w14(1, 2, 3); }
int w15(float f) { return w15(1.5); }
int w16(void) { unsigned u = -1; int i = 4294967296LL; return u + i; }
int w17(int x) { return (x & 0) == 1 || (u8)x > 1000; }
static int unused_static(void) { return 1; }
int w18(void) { struct S *p = (struct S *)&u1; return p->a; }
int w19(int x, ...) { return x; }
int w20(void) { return w19(1, 2.0f, (char)3, (short)4, 5LL); }
int w21(void) { int a[4]; return a[10] + a[-1]; }
int w22(void) { long long big = 0xFFFFFFFFFFFFFFFFULL; return big >> 70; }
int w23(void) { return sizeof(void) + sizeof(w1); }
int w24(int x) { return x ? : 7; }
int w25(void) { enum E { A = 1, B = 0x80000000 } e = B; return e; }
int dup_decl; int dup_decl; int w26(void) { extern char dup_decl; return dup_decl; }
