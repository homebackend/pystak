[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
[![LinkedIn][linkedin-shield]][linkedin-url]

# About pystak
**pystak** is a command line tool to download books from various sources written in [Python 3](https://www.python.org/). Currently the following sources are supported:
- [Anna's Archive](https://annas-archive.org/)
- [Internet Archive](https://archive.org/)
- [Indian Culture](https://indianculture.gov.in/)
- [Telegram](https://telegram.org/) Channel

This tool allows both searching and downloading of books from the above sources. More sources can be added in future. Feel free to raise a an issue or pull request as the case may be.


# Installation
## Requirements

**pystak** requires *python 3* and a bunch of other dependencies mentioned in `requirements.txt` file.

## Setup
Before running **pystak** the python environment needs to be setup correctly. Here we are creating a python virtual environment and installing all the dependencies. The instructions are provided for *Linux*, but ideally these should be identical for any *UNIX* like operating system.

### Create virtual environment and install dependencies

The following Change to the folder/directory containing 

```bash
python -m venv venv
. venv/bin/activate
pip install -r requirements.txt
```
### Activating virtual environment

Creating virtual environment and installing dependencies is one time process. In subsequent runs you just need to activate the virtual environment:
```bash
. venv/bin/activate
```

To deactivate the virtual environment run the command: `deactivate`.

# Usage

## Basic Usage

**pystak** supports search and download subcommands which allow searching and downloading of books respectively. Different sources may support additional sub commands.
**pystak** command exits only after all files have been downloaded. You can exit from the command at any time by pressing `ctrl+c`. **pystak** starts to resume downloading from where it left on subsequent reissuing of the same command.

A summary of the entire process is printed when the process exits.

### Downloading books

If you have bunch of book download URLs handy, you can download books from a *source* by issuing the command: 
```bash
python3 pystak.py <source> download <url-1> [<url-2> ...]
```

Following is a collection of sample command for each supported *source*:

- [Anna's Archive](https://annas-archive.org/): 
  ```bash 
  python3 pystak.py annas download https://annas-archive.org/md5/e34b90617bbb35cc4ea90180cfc93798
  ```
- [Internet Archive](https://archive.org/): 
  ```bash
  python3 pystak.py archive download https://archive.org/details/in.ernet.dli.2015.12873
  ```
- [Indian Culture](https://indianculture.gov.in/): 
  ```bash
  python3 pystak.py indian-culture download https://indianculture.gov.in/archives/question-treatment-approver-delhi-lahore-conspiracy-case
  ```
- [Telegram](https://telegram.org/) Channel: 
  ```bash
  python3 pystak.py telegram -i 12345678 -a 0123456789abcdef0123456789abcdef -p +911234567890 download -c @PLANDEMIC message-id
  ```
  In the above command:
  - `-i` is the api_id
  - `-a` is the api_hash
  - `-p` is telegram phone number
  - `-c` is the telegram channel user name
  
  Typically, `search` is more useful subcommand in case telegram. `download` is used directly only if you know the message ids beforehand.
  You can create your own telegram application from [here](https://my.telegram.org/)

### Searching books

To search for a book from a given *source* containing *search text* in title issue the command: 
```bash
python3 pystak.py <source> search search text
```
Once search is over, you will be presented with a list of books from which you can select a bunch of books to download.

Following is a collection of sample command for each supported *source*:

- [Anna's Archive](https://annas-archive.org/): 
  ```bash 
  python3 pystak.py annas search economic and political weekly
  ```
- [Internet Archive](https://archive.org/): 
  ```bash
  python3 pystak.py archive search CWMG
  ```
- [Indian Culture](https://indianculture.gov.in/): 
  ```bash
  python3 pystak.py indian-culture search kakori conspiracy dase
  ```
- [Telegram](https://telegram.org/) Channel: 
  ```bash
  python3 pystak.py telegram -i 12345678 -a 0123456789abcdef0123456789abcdef -p +911234567890 search -c @channel search text
  ```
  In case of telegram, **pystak** will search for *search text* within all the messages of the specified *@channel* having a pdf file as attachment whose file name contains the string *search text*, and will not search within the message text itself,

### Availing help

For more information, command line option `-h` or `--help` can be used.

## Source specific comments

### Anna's Archive

In case of [Anna's Archive](https://annas-archive.org/), books are downloaded using [torrent](https://en.wikipedia.org/wiki/BitTorrent) file. As a result the actual download can take long time dependening upon the availability of [peers](https://en.wikipedia.org/wiki/Glossary_of_BitTorrent_terms#Peer).
As a result **pystak** will continue running for long time. If you want to download more files from [Anna's Archive](https://annas-archive.org/) in the meantime, issue appropriate command in another command line console. The new files will be added to long running process via RPC calls.
At any time feel free to press ctrl+c to stop download. A subsequent execution of the same command as before should enable resuming of download from where it started.

#### Status command

Issue the command 
```bash
python3 pystak.py annas status
```
to check whether another **pystak** command is executing somewhere.

### Internet archive

The download is fully resumable on running the command again.

For some files you might see a message like: `access to item is restricted`. Such files are either not downloadable or require login.
When downloadable use the following tool: https://github.com/MiniGlome/Archive.org-Downloader to download these files.

### Indian Culture

In case of [Indian Culture](https://indianculture.gov.in/), an additional `list` command is supported. See command line help for more details. A sample command:
```bash
python3 pystak.py indian-culture list https://indianculture.gov.in/freedom-archive/rarebooks
```
Downloads are not resumable but fully downloaded files are not downloaded again.

### Telegram

Downloads are not resumable but fully downloaded files are not downloaded again.


# Etymology

The word pustak in Hindi means book.

py<s>thon</s> + <s>pu</s>stak => pystak


<!-- MARKDOWN LINKS & IMAGES -->
<!-- https://www.markdownguide.org/basic-syntax/#reference-style-links -->
[contributors-shield]: https://img.shields.io/github/contributors/homebackend/pystak.svg?style=for-the-badge
[contributors-url]: https://github.com/homebackend/pystak/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/homebackend/pystak.svg?style=for-the-badge
[forks-url]: https://github.com/homebackend/pystak/network/members
[stars-shield]: https://img.shields.io/github/stars/homebackend/pystak.svg?style=for-the-badge
[stars-url]: https://github.com/homebackend/pystak/stargazers
[issues-shield]: https://img.shields.io/github/issues/homebackend/pystak.svg?style=for-the-badge
[issues-url]: https://github.com/homebackend/pystak/issues
[license-shield]: https://img.shields.io/github/license/homebackend/pystak.svg?style=for-the-badge
[license-url]: https://github.com/homebackend/pystak/blob/master/LICENSE
[linkedin-shield]: https://img.shields.io/badge/-LinkedIn-black.svg?style=for-the-badge&logo=linkedin&colorB=555
[linkedin-url]: https://linkedin.com/in/neeraj-jakhar-39686212b

