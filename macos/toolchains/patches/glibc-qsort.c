/* qsort with glibc's ordering, linked into the compiler so it replaces the macOS one.

   These compilers sort arrays whose comparison functions call some items equal (and some aren't even
   consistent), so the final order depends on the sorting algorithm, and that order ends up in the generated
   code (register allocation, stack slots). The Linux builds everyone matches against use glibc's qsort,
   a top-down merge sort that keeps the left item on ties; macOS's qsort is a different algorithm. This file
   is the same merge, written independently: halves of n/2 and n - n/2, merged taking the left item while
   cmp(left, right) <= 0. */

#include <stdlib.h>
#include <string.h>

static void msort (char *b, size_t n, size_t s, int (*cmp) (const void *, const void *), char *t)
{
  size_t n1, n2;
  char *b1, *b2, *tmp;

  if (n <= 1)
    return;
  n1 = n / 2;
  n2 = n - n1;
  b1 = b;
  b2 = b + n1 * s;
  msort (b1, n1, s, cmp, t);
  msort (b2, n2, s, cmp, t);

  tmp = t;
  while (n1 > 0 && n2 > 0)
    {
      if ((*cmp) (b1, b2) <= 0)
        {
          memcpy (tmp, b1, s);
          b1 += s;
          --n1;
        }
      else
        {
          memcpy (tmp, b2, s);
          b2 += s;
          --n2;
        }
      tmp += s;
    }
  if (n1 > 0)
    memcpy (tmp, b1, n1 * s);
  memcpy (b, t, (n - n2) * s);
}

void qsort (void *b, size_t n, size_t s, int (*cmp) (const void *, const void *))
{
  char *t;

  if (n <= 1)
    return;
  t = malloc (n * s);
  if (t == NULL)
    abort ();
  msort (b, n, s, cmp, t);
  free (t);
}
