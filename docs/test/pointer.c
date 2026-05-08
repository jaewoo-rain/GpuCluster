#include <stdio.h>

int add(int a, int b){
    return a + b;
}

int main(void){
    int (*f)(int, int);
    f = &add;
    int r = f(2, 3);
    printf("%d\n", r);

    return 0;
}