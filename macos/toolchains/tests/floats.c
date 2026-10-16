typedef float f32; typedef double f64;
f32 fa = 1.0f/3.0f, fb = 0.1f + 0.2f, fc = 3.4028234e38f, fd = 1.17549435e-38f, fe = 1e-45f, ff = 16777217.0f;
f64 da = 1.0/3.0, db = 0.1 + 0.2, dc = 1.7976931348623157e308, dd = 2.2250738585072014e-308, de = 4.9e-324;
f64 df = 9007199254740993.0, dg = 1e300 * 1e10, dh = -0.0, di = 1.0/7.0*7.0;
f32 conv1 = (f32)(1.0/3.0); f32 conv2 = (f32)16777217; int conv3 = (int)2.9999999; unsigned conv4 = (unsigned)3e9;
f64 tbl[8] = { 3.14159265358979323846, 2.71828182845904523536, 1.41421356237309504880, 0.57721566490153286, 1e-5, 1e5, 123456789.123456789, -987654321.5 };
f32 ftbl[8] = { 3.14159265f, 2.7182818f, 1.4142135f, 0.5772156f, 1e-5f, 1e5f, 123456789.1f, -0.33333334f };
f32 fsc(f32 x) { return x * 0.017453292f + 1.0f / 60.0f - x / 3.0f; }
f64 dsc(f64 x) { return x * 0.017453292519943295 + 1.0 / 60.0 - x / 3.0 + 2.0 * 1e-17; }
f32 fmix(int i, f32 a, f64 b) { return (f32)(i * a + b) * 0.5f - (f32)i / 7.0f; }
f32 lerp(f32 a, f32 b, f32 t) { return a + (b - a) * t; }
int toi(f32 x) { return (int)(x * 4096.0f); }
f32 fromi(int x) { return x / 4096.0f; }
f64 poly(f64 x) { return ((((0.0083333 * x - 0.1666666) * x) * x + 1.0) * x); }
f32 cfold(void) { return 0.1f * 3.0f - 0.3f + (1.0f/3.0f)*3.0f + 1e20f*1e20f*0.0f; }
f64 dfold(void) { return 0.1 * 3.0 - 0.3 + (1.0/3.0)*3.0 + 1e-320 * 1e-10 + 1e308 * 10.0 - 1e308 * 10.0; }
f32 sqrt_guess(f32 x) { f32 g = x * 0.5f; int k; for (k = 0; k < 5; k++) g = 0.5f * (g + x / g); return g; }
int cmpf(f32 a, f32 b) { return (a < b) - (a > b) + (a == b) * 2; }
