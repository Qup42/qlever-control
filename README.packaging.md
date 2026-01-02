```bash
# Build the container for packaging. You can also install the required packages directly on your system.
docker build -t packaging -f Dockerfile.packaging .
docker run --rm -it -v .:/work -w /work -v ./packaged:/output packaging bash
# Build the package.
debuild -us -uc
# The output files are `/qlever-control_<version>_<arch>*`. Move them to the output directory.
mv /qlever-control_0.5.36-2* /output/
# For the repository we need the `.deb`, `.dsc`, and `.changes` files.
```
