// Read a file over and over: open -> read everything -> close, repeat.
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	if (argc < 2) {
		fprintf(stderr, "usage: %s <file> [iterations]\n", argv[0]);
		return 1;
	}

	const char *path = argv[1];
	/* 0 (or omitted) means loop forever. */
	long iterations = (argc > 2) ? strtol(argv[2], NULL, 10) : 0;

	char buf[64 * 1024];

	for (long i = 0; iterations == 0 || i < iterations; i++) {
		int fd = open(path, O_RDWR);
		if (fd < 0) {
			fprintf(stderr, "open %s: %s\n", path, strerror(errno));
			return 1;
		}

		ssize_t n;
		while ((n = read(fd, buf, sizeof(buf))) > 0)
			; /* discard; we just want to read everything */

		if (n < 0) {
			fprintf(stderr, "read %s: %s\n", path, strerror(errno));
			close(fd);
			return 1;
		}

		close(fd);
	}

	return 0;
}
