"""Creating, matching, and otherwise manipulating stories within topics."""

import datetime
import operator
import re
import typing

from mediawords.db import DatabaseHandler
from mediawords.tm.guess_date import guess_date, GuessDateResult
from mediawords.util.log import create_logger
import mediawords.util.url

log = create_logger(__name__)


# Giving up on porting the extraction stuff for now because it requires porting all the way down to StoryVectors.pm.
# Will leave the extraction to the perl side Mine.pm code.
# -hal
# def queue_download_exrtaction(db: DatabaseHandler, download: dict) -> None:
#     """Queue an extraction job for the download
#
#     This just adds some checks not to re-extract and not to download obvious big binary file types.
#     """
#
#     for ext in 'jpg pdf doc mp3 mp4 zip l7 gz'.split():
#         if download['url'].endswith(ext):
#             return
#
#     if re.search(r'livejournal.com\/(tag|profile)', download['url'], flags=re.I) is not None:
#         return
#
#     dt = db.query("select 1 from download_texts where downloads_id = %(a)s", {'a': download['downloads_id']}).hash()
#     if dt is not None:
#         return
#
#     try:
#         mediawords.dbi.downloads.process_download_for_extractor(
#             db=db, download=download, use_cache=True, no_dedup_sentences=False)
#     except Exception as e:
#         log.warning("extract error processing download %d: %s" % (download['downloads_id'], str(e)))


def _get_story_with_most_sentences(db: DatabaseHandler, stories: list) -> dict:
    """Given a list of stories, return the story with the most sentences."""
    assert len(stories) > 0

    if len(stories) == 1:
        return stories[0]

    (stories_id) = db.query(
        """
        select stories_id
            from story_sentences
            where stories_id in (%(a)s)
            group by stories_id
            order by count(*) desc
            limit 1
        """,
        {'a', [s['stories_id'] for s in stories]}).flat()

    for story in stories:
        if story['stories_id'] == stories_id:
            return story

    assert False, "unable to find stories_id in stories list"


def _story_domain_matches_medium(db: DatabaseHandler, medium: dict, urls: list) -> bool:
    """Return true if the domain of any of the story urls matches the domain of the medium url."""
    medium_domain = mediawords.util.url.get_url_distinctive_domain(medium['url'])

    story_domains = [mediawords.util.url.get_url_distinctive_domain(u) for u in urls]

    matches = list(filter(lambda d: medium_domain == d, story_domains))

    return len(matches) > 0


def get_preferred_story(db: DatabaseHandler, urls: list, stories: list) -> dict:
    """Given a set of possible story matches, find the story that is likely the best to include in the topic.

    The best story is the one that first belongs to the media source that sorts first according to the following
    criteria, in descending order of importance:

    * media pointed to by some dup_media_id
    * media without a dup_media_id
    * media whose url domain matches that of the story
    * media with a lower media_id

    Within a media source, the preferred story is the one with the most sentences.

    Arguments:
    db - db handle
    url - url of matched story
    redirect_url - redirect_url of matched story
    stories - list of stories from which to choose

    Returns:
    a single preferred story

    """
    assert len(stories) > 0

    stories = list(set(stories))

    if len(stories) == 1:
        return stories[0]

    log.debug("get_preferred_story: %d stories" % len(stories))

    media = db.query(
        """
        select *,
                exists ( select 1 from media d where d.dup_media_id = m.media_id ) is_dup_target
            from media
            where media_id in (%(a)s)
        """,
        {'a': [s['stories_id'] for s in stories]}).hashes()

    for medium in media:
        # is_dup_target defined in query above
        medium['is_not_dup_source'] = 0 if medium['dup_media_id'] else 1
        medium['matches_domain'] = 1 if _story_domain_matches_medium(db, medium, urls) else 0
        medium['stories'] = filter(lambda s: s['media_id'] == medium['media_id'], stories)

    sorted_media = sorted(
        media,
        key=operator.attrgetter('is_dup_target', 'is_not_dup_source', 'matches_domain', 'media_id'))

    preferred_story = _get_story_with_most_sentences(db, sorted_media[0]['stories'])

    log.debug("get_preferred_story done")

    return preferred_story


def generate_medium_url_and_name_from_url(story_url: str) -> tuple:
    """Derive the url and a media source name from a story url.

    This function just returns the pathless normalized url as the medium_url and the host nane as the medium name.

    Arguments:
    url - story url

    Returns:
    tuple in the form (medium_url, medium_name)

    """
    normalized_url = mediawords.util.url.normalize_url_lossy(story_url)
    if normalized_url is None:
        return (story_url, story_url)

    matches = re.search(r'(http.?://([^/]+))', normalized_url, flags=re.I)
    if matches is None:
        log.warning("Unable to find host name in url: normalized_url (%(a)s)" % story_url)
        return (story_url, story_url)

    (medium_url, medium_name) = (matches.group(1).lower(), matches.group(2).lower())

    if not medium_url.endswith('/'):
        medium_url += "/"

    return (medium_url, medium_name)


def ignore_redirect(db: DatabaseHandler, url: str, redirect_url: typing.Optional[str]) -> bool:
    """Return true if we should ignore redirects to the target media source.

    This is usually to avoid redirects to domainresellers for previously valid and important but now dead links"""
    if redirect_url is None or url == redirect_url:
        return False

    medium_url = generate_medium_url_and_name_from_url(redirect_url)[0]

    u = mediawords.util.url.normalize_url_lossy(medium_url)

    match = db.query("select 1 from topic_ignore_redirects where url = %(a)s", {'a': u}).hash()

    return match is not None


def get_story_match(db: DatabaseHandler, url: str, redirect_url: typing.Optional[str]=None) -> typing.Optional[dict]:
    """Search for any story within the database that matches the given url.

    Searches for any story whose guid or url matches either the url or redirect_url or the
    mediawords.util.url.normalized_url_lossy() version of either.

    If multiple stories are found, use get_preferred_story() to decide which story to return.

    Only mach the first 1024 characters of the url / redirect_url.

    Arguments:
    db - db handle
    url - story url
    redirect_url - optional url to which the story url redirects

    Returns:
    the matched story or None

    """
    u = url[0:1024]

    ru = ''
    if not ignore_redirect(db, url, redirect_url):
        ru = redirect_url[0:1024] if redirect_url is not None else u

    nu = mediawords.util.url.normalize_url_lossy(u)
    nru = mediawords.util.url.normalize_url_lossy(ru)

    urls = list(set((u, ru, nu, nru)))

    # look for matching stories, ignore those in foreign_rss_links media
    stories = db.query(
        """
select distinct(s.*) from stories s
        join media m on s.media_id = m.media_id
    where
        ( ( s.url in ( %(a)s ) ) or
            ( s.guid in ( %(a)s ) ) ) and
        m.foreign_rss_links = false

union

select distinct(s.*) from stories s
        join media m on s.media_id = m.media_id
        join topic_seed_urls csu on s.stories_id = csu.stories_id
    where
        csu.url in ( %(a)s ) and
        m.foreign_rss_links = false
        """,
        {'a': urls}).hashes()

    story = get_preferred_story(db, urls, stories)

    return story


def create_download_for_new_story(db: DatabaseHandler, story: dict, feed: dict) -> dict:
    """Create and return download object in database for the new story."""

    download = {
        'feeds_id': feed['feeds_id'],
        'stories_id': story['stories_id'],
        'url': story['url'],
        'host': mediawords.util.url.get_url_host(story['url']),
        'type': 'content',
        'sequence': 1,
        'state': 'success',
        'path': 'content:pending',
        'priority': 1,
        'extracted': 'f'
    }

    download = db.create('downloads', download)

    return download


def assign_date_guess_tag(
        db: DatabaseHandler,
        story: dict,
        date_guess: GuessDateResult,
        fallback_date: typing.Optional[str]) -> None:
    """Assign a guess method tag to the story based on the date_guess result.

    If date_guess found a result, assing a date_guess_method:guess_by_url, guess_by_tag_*, or guess_by_uknown tag.
    Otherwise if there is a fallback_date, assign date_guess_metehod:fallback_date.  Else assign
    date_invalid:date_invalid.

    Arguments:
    db - db handle
    story - story dict from db
    date_guess - GuessDateResult from guess_date() call

    Returns:
    None

    """
    if date_guess.found():
        tag_set = 'date_guess_method'
        guess_method = date_guess.guess_method()
        if guess_method.startswith('Extracted from url'):
            tag = 'guess_by_url'
        elif guess_method.startswith('Extracted from tag'):
            match = re.search(r'\<(\w+)', guess_method)
            html_tag = match.group(1) if match is not None else 'unknown'
            tag = 'guess_by_tag_' + str(html_tag)
        else:
            tag = 'guess_by_unknown'
    elif fallback_date is not None:
        tag_set = 'date_guess_method'
        tag = 'fallback_date'
    else:
        tag_set = 'date_invalid'
        tag = 'date_invalid'

    ts = db.find_or_create('tag_sets', {'name': tag_set})
    t = db.find_or_create('tags', {'tag': tag, 'tag_sets_id': ts['tag_sets_id']})

    db.create('stories_tags_map', {'stories_id': story['stories_id'], 'tags_id': t['tags_id']})


def add_new_story(
        db: DatabaseHandler,
        url: str,
        content: str,
        fallback_date: typing.Optional[datetime.datetime]=None) -> dict:
    """Add a new story to the database by guessing metadata using the given url and content.


    This function guesses the medium, feed, title, and date of the story from the url and content.

    Arguments:
    db - db handle
    url - story url
    content - story content
    fallback_date - fallback to this date if the date guesser fails to find a date
    """

    url = url[0:1024]

    medium = get_spider_medium(db, url)
    feed = get_spider_feed(db, medium)
    spidered_tag = get_spidered_tag(db)
    title = mediawords.util.html.html_title(content, url, 1024)

    story = {
        'url': url,
        'guid': url,
        'media_id': medium['media_id'],
        'collect_date': datetime.datetime.now(),
        'title': title,
        'description': ''
    }

    # postgres refuses to insert text values with the null character
    for field in ('url', 'guid', 'title'):
        story[field] = re.sub('\x00', '', story[field])

    date_guess = guess_date(url, content)
    story['publish_date'] = date_guess.date() if date_guess.found() else fallback_date
    if story['publish_date'] is None:
        story['publish_date'] = datetime.datetime.now().isoformat()

    story = db.create('stories', story)

    db.create('stories_tags_map', {'stories_id': story['stories_id'], 'tags_id': spidered_tag['tags_id']})

    assign_date_guess_tag(db, story, date_guess, fallback_date)

    log.debug("add story: %s; %s; %s; %d" % (story['title'], story['url'], story['publish_date'], story['stories_id']))

    db.create('feeds_stories_map', {'stories_id': story['stories_id'], 'feeds_id': feed['feeds_id']})

    download = create_download_for_new_story(db, story, feed)

    download = mediawords.dbi.downloads.store_content(db, download, content)

    return story