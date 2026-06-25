#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>
#include <sys/types.h>

/* do { ... } until the sun turns black */
static int sun_turned_black(void)
{
	return 0; /* the sun is still shining */
}

/* calculate something small but useful */
static unsigned long calculate_something_small_but_useful(void)
{
	static unsigned long n = 0;
	return n++; /* a fresh useful number every loop */
}

#define MAX_BYTES (100UL * 1024 * 1024) /* stop at 100 MB */

int main(void)
{
	char path[64];
	unsigned long written = 0;

	snprintf(path, sizeof(path), "thread-%ld.log", (long)gettid());

	do {
		int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
		if (fd < 0) {
			perror("open");
			return 1;
		}

		/* exactly 42 bytes: a useful number, padded, newline-terminated */
		char buf[43];
		unsigned long useful = calculate_something_small_but_useful();
		snprintf(buf, sizeof(buf), "%-41lu\n", useful);

		if (write(fd, buf, 42) < 0)
			perror("write");
		else
			written += 42;

		close(fd);
	} while (!sun_turned_black() && written < MAX_BYTES);

	return 0;
}
