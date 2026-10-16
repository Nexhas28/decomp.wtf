/* Prototypes for GCC 2.x's variadic diagnostics. Many files call them without a declaration in scope; on
   Apple arm64 an unprototyped call passes arguments in registers while a variadic function reads them from
   the stack, so without these the compiler crashes (or prints garbage) when it reports some warnings. */
#include <stdio.h>
union tree_node;
struct rtx_def;
void error (char *, ...);
void warning (char *, ...);
void pedwarn (char *, ...);
void fatal (char *, ...);
void sorry (char *, ...);
void really_sorry (char *, ...);
void error_with_file_and_line (char *, int, char *, ...);
void warning_with_file_and_line (char *, int, char *, ...);
void pedwarn_with_file_and_line (char *, int, char *, ...);
void error_with_decl (union tree_node *, char *, ...);
void warning_with_decl (union tree_node *, char *, ...);
void pedwarn_with_decl (union tree_node *, char *, ...);
void error_for_asm (struct rtx_def *, char *, ...);
void warning_for_asm (struct rtx_def *, char *, ...);
void asm_fprintf (FILE *, char *, ...);
