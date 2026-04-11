Howdy y'all,

Wersi is a German manufacturer of electronic organs and pianos.  Their
antique SL-M2 NR.51173 module contains two Zilog Z0861112PSC
microcontrollers with programs in diffusion ROM.  My buddy David
Ryskalczyk politely asked me to photograph that ROM for extraction,
and this repo contains the partial result.

The ROM is advertised as 4kB, but it is arranged as 130 rows and 256
columns totaling 33,280 bits.  The extra 512 bits hold a test ROM that
is independent of the application.  This test ROM is documentation by
Zilog.

To edit the bit markings, simply open `rom10x.bmp` in [Mask ROM
Tool](https://github.com/travisgoodspeed/maskromtool/).  `make
rom10x.txt` will produce an ASCII art of the bits in physical order.

`make clean rom10x.bin` will produce a binary file.  It has some bit errors,
but we are working to correct them in [this issue](https://github.com/travisgoodspeed/wersi-slm2-51173/issues/2).

Happy hunting,

--Travis Goodspeed

