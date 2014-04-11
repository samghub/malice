from flask import render_template, url_for, redirect, g, abort, request, jsonify

from app.api.metascan_api import MetaScan
from app.worker.av.avast.scanner import avast_engine
from app.worker.av.avira.scanner import avira_engine
from app.worker.av.bitdefender.scanner import bitdefender_engine
from app.worker.av.clamav.scanner import clamav_engine
from app.worker.av.eset.scanner import eset_engine
from app.worker.av.kaspersky.scanner import kaspersky_engine
from app.worker.av.sophos.scanner import sophos_engine
from app.worker.av.avg import scanner as avg_engine
from app.worker.av.f_prot import scanner as f_prot_engine
from app.worker.file.doc import pdf
from app.worker.file.doc.pdf.pdfid import pdfid
from app.worker.file.doc.pdf.pdfid.scanner import pdfid_engine
from app.worker.file.doc.pdf.pdfparser import pdfparser
from app.worker.file.exe.pe import pe
from app.worker.file.exif import exif
from app.worker.file.trid import trid

from app.worker.intel.bit9 import single_query_bit9, batch_query_bit9
from app.worker.intel.virustotal import single_query_virustotal, batch_query_virustotal

import ConfigParser
from dateutil import parser
from lib.common.out import *
from lib.core.database import is_hash_in_db, db_insert
from lib.scanworker.file import PickleableFileSample

import hashlib
import rethinkdb as r

from redis import Redis
from rq import Queue
from rq.decorators import job

from app import app

__author__ = 'Josh Maine'

q = Queue('low', connection=Redis())

config = ConfigParser.ConfigParser()
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
config.read(os.path.join(BASE_DIR, '../conf/config.cfg'))

# TODO : Make it so that it will scan with every available worker instead of having to do it explicitly
def scan_upload(file_stream, sample):
    # job = q.enqueue(run_workers, file_stream)
    # print job.result
    print("<< Now Scanning file: {}  >>>>>>>>>>>>>>>>>>>>>>>>>>>>".format(sample['filename']))
    print_info("Scanning with MetaScan now.")
    if run_metascan(file_stream, sample['md5']):
        print_success("MetaScan Complete.")
    #: Run the AV workers on the file.
    print_info("Scanning with AV workers now.")
    if run_workers(file_stream):
        print_success("Malice AV scan Complete.")
    print_info("Performing file analysis now.")
    print_item("Scanning with EXIF now.", 1)
    exif_scan(file_stream, sample['md5'])
    print_item("Scanning with TrID now.", 1)
    trid_scan(file_stream, sample['md5'])
    if file_is_pe(file_stream):
        #: Run PE Analysis
        print_item("Scanning with PE Analysis now.", 1)
        pe_scan(file_stream, sample['md5'])
    if file_is_pdf(file_stream):
        #: Run PDF Analysis
        pdfparser_scan(file_stream, sample['md5'])
        pdfid_scan(file_stream, sample['md5'])
    #: Run Intel workers
    print_item("Searching for Intel now.", 1)
    single_hash_search(sample['md5'])
    print_success("File Analysis Complete.")
    found = is_hash_in_db(sample['md5'])
    return found['user_uploads'][-1]['detection_ratio']


def single_hash_search(this_hash):
    found = is_hash_in_db(this_hash)
    if not found:
        #: Run all Intel Workers on hash
        # TODO: Make these async with .delay(this_hash)
        single_query_bit9(this_hash)
        single_query_virustotal(this_hash)
        return is_hash_in_db(this_hash)
    else:  #: Fill in the blanks
        if 'Bit9' not in list(found.keys()):
            single_query_bit9(this_hash)
        if 'VirusTotal' not in list(found.keys()):
            single_query_virustotal(this_hash)
        if found:
            # TODO: handle case where all fields are filled out (session not updating on first submission)
            r.table('sessions').insert(found).run(g.rdb_sess_conn)
            return found
        else:
            return False


def batch_search_hash(hash_list):
    new_hash_list = []
    search_results = []
    #: Check DB for hashes, if found do not query API
    for a_hash in hash_list:
        found = is_hash_in_db(a_hash)
        if found:
            search_results.append(found)
            r.table('sessions').insert(found).run(g.rdb_sess_conn)
        else:
            new_hash_list.append(a_hash)

    if new_hash_list:
        batch_query_bit9(new_hash_list)
        # batch_query_bit9.delay(new_hash_list)
        batch_query_virustotal(new_hash_list)
        for a_new_hash in new_hash_list:
            found = is_hash_in_db(a_new_hash)
            if found:
                search_results.append(found)
        return search_results
    else:
        return search_results


def scan_to_dict(scan, av):
    return dict(av=av, digest=scan.digest, infected=scan.infected, infected_string=scan.infected_string,
                metadata=scan.metadata, timestamp=r.now())
# def scan_to_dict(scan, av):
#     return dict(av=av, digest=scan.digest, infected=scan.infected, infected_string=scan.infected_string,
#                 metadata=scan.metadata, timestamp=r.now())


def avast_scan(file):
    my_avast_engine_engine = avast_engine()
    result = my_avast_engine_engine.scan(PickleableFileSample.string_factory(file))
    file_md5_hash = hashlib.md5(file).hexdigest().upper()
    found = is_hash_in_db(file_md5_hash)
    if found:
        found['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(result, 'Avast'))
        if result.infected:
            found['user_uploads'][-1]['detection_ratio']['infected'] += 1
        found['user_uploads'][-1]['detection_ratio']['count'] += 1
        data = found
    else:
        data = dict(md5=file_md5_hash)
        data['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(result, 'Avast'))
    db_insert(data)
    return data


def avg_scan(file):
    my_avg = avg_engine.AVG(file)
    result = my_avg.scan()
    # result = my_avg.scan(PickleableFileSample.string_factory(file))
    file_md5_hash = hashlib.md5(file).hexdigest().upper()
    found = is_hash_in_db(file_md5_hash)
    if found:
        found['user_uploads'][-1].setdefault('av_results', []).append(result[1])
        if result[1]['infected']:
            found['user_uploads'][-1]['detection_ratio']['infected'] += 1
        found['user_uploads'][-1]['detection_ratio']['count'] += 1
        data = found
    else:
        data = dict(md5=file_md5_hash)
        data['user_uploads'][-1].setdefault('av_results', []).append(result[1])
    db_insert(data)
    return data


# TODO: metadata contains a element (description) that I should append or replace the infected string with
# TODO: [^cont.] i.e. 'Contains code of the Eicar-Test-Signature virus' vs. 'Eicar-Test-Signature'
def avira_scan(file):
    my_avira_engine = avira_engine()
    result = my_avira_engine.scan(PickleableFileSample.string_factory(file))
    file_md5_hash = hashlib.md5(file).hexdigest().upper()
    found = is_hash_in_db(file_md5_hash)
    if found:
        found['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(result, 'Avira'))
        if result.infected:
            found['user_uploads'][-1]['detection_ratio']['infected'] += 1
        found['user_uploads'][-1]['detection_ratio']['count'] += 1
        data = found
    else:
        data = dict(md5=file_md5_hash)
        data['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(result, 'Avira'))
    db_insert(data)
    return data


def eset_scan(file):
    my_eset_engine = eset_engine()
    result = my_eset_engine.scan(PickleableFileSample.string_factory(file))
    file_md5_hash = hashlib.md5(file).hexdigest().upper()
    found = is_hash_in_db(file_md5_hash)
    if found:
        found['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(result, 'ESET-NOD32'))
        if result.infected:
            found['user_uploads'][-1]['detection_ratio']['infected'] += 1
        found['user_uploads'][-1]['detection_ratio']['count'] += 1
        data = found
    else:
        data = dict(md5=file_md5_hash)
        data['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(result, 'ESET-NOD32'))
    db_insert(data)
    return data


def bitdefender_scan(file):
    bitdefender = bitdefender_engine()
    result = bitdefender.scan(PickleableFileSample.string_factory(file))
    file_md5_hash = hashlib.md5(file).hexdigest().upper()
    found = is_hash_in_db(file_md5_hash)
    if found:
        found['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(result, 'BitDefender'))
        if result.infected:
            found['user_uploads'][-1]['detection_ratio']['infected'] += 1
        found['user_uploads'][-1]['detection_ratio']['count'] += 1
        data = found
    else:
        data = dict(md5=file_md5_hash)
        data['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(result, 'BitDefender'))
    db_insert(data)
    return data


def clamav_scan(file):
    my_clamav_engine = clamav_engine()
    results = my_clamav_engine.scan(PickleableFileSample.string_factory(file))
    file_md5_hash = hashlib.md5(file).hexdigest().upper()
    found = is_hash_in_db(file_md5_hash)
    if found:
        found['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(results, 'ClamAV'))
        if results.infected:
            found['user_uploads'][-1]['detection_ratio']['infected'] += 1
        found['user_uploads'][-1]['detection_ratio']['count'] += 1
        data = found
    else:
        data = dict(md5=file_md5_hash)
        data['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(results, 'ClamAV'))
    db_insert(data)
    return data


def f_prot_scan(file):
    my_f_prot = f_prot_engine.F_PROT(file)
    result = my_f_prot.scan()
    # result = my_avg.scan(PickleableFileSample.string_factory(file))
    file_md5_hash = hashlib.md5(file).hexdigest().upper()
    # found = is_hash_in_db(file_md5_hash)
    # if found:
    #     found['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(result, 'AVG'))
    #     if result.infected:
    #         found['user_uploads'][-1]['detection_ratio']['infected'] += 1
    #     found['user_uploads'][-1]['detection_ratio']['count'] += 1
    #     data = found
    # else:
    #     data = dict(md5=file_md5_hash)
    #     data['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(result, 'AVG'))
    # db_insert(data)
    # return data


def kaspersky_scan(file):
    my_kaspersky_engine = kaspersky_engine()
    results = my_kaspersky_engine.scan(PickleableFileSample.string_factory(file))
    file_md5_hash = hashlib.md5(file).hexdigest().upper()
    found = is_hash_in_db(file_md5_hash)
    if found:
        found['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(results, 'Kaspersky'))
        if results.infected:
            found['user_uploads'][-1]['detection_ratio']['infected'] += 1
        found['user_uploads'][-1]['detection_ratio']['count'] += 1
        data = found
    else:
        data = dict(md5=file_md5_hash)
        data['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(results, 'Kaspersky'))
    db_insert(data)
    return data


def sophos_scan(file):
    my_sophos = sophos_engine()
    results = my_sophos.scan(PickleableFileSample.string_factory(file))
    file_md5_hash = hashlib.md5(file).hexdigest().upper()
    found = is_hash_in_db(file_md5_hash)
    if found:
        found['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(results, 'Sophos'))
        if results.infected:
            found['user_uploads'][-1]['detection_ratio']['infected'] += 1
        found['user_uploads'][-1]['detection_ratio']['count'] += 1
        data = found
    else:
        data = dict(md5=file_md5_hash)
        data['user_uploads'][-1].setdefault('av_results', []).append(scan_to_dict(results, 'Sophos'))
    db_insert(data)
    return data


def exif_scan(file_stream, file_md5):
    this_exif = exif.Exif(file_stream)
    if this_exif:
        key, exif_results = this_exif.scan()
        found = is_hash_in_db(file_md5)
        if found:
            found[key] = exif_results
            data = found
        else:
            data = dict(md5=file_md5)
            data[key] = exif_results
        db_insert(data)
        return data
    else:
        print_error("EXIF Analysis Failed.")


def trid_scan(file_stream, file_md5):
    this_trid = trid.TrID(file_stream)
    if this_trid:
        key, trid_results = this_trid.scan()
        found = is_hash_in_db(file_md5)
        if found:
            found[key] = trid_results
            data = found
        else:
            data = dict(md5=file_md5)
            data[key] = trid_results
        db_insert(data)
        return data
    else:
        print_error("TrID Analysis Failed.")


def pe_scan(file, file_md5):
    this_pe = pe.PE(file)
    if this_pe.pe:
        key, pe_results = this_pe.scan()
        found = is_hash_in_db(file_md5)
        if found:
            found[key] = pe_results
            data = found
        else:
            data = dict(md5=file_md5)
            data[key] = pe_results
        db_insert(data)
        return data
    else:
        print_error("PE Analysis Failed - This file might not be a PE Executable.")


def file_is_pdf(file):
    return False


def file_is_pe(file):
    return True


def pdfparser_scan(file, file_md5):
    this_pdf = pdfparser.PdfParser(file)
    if this_pdf:
        key, pe_results = this_pdf.scan()
        found = is_hash_in_db(file_md5)
        if found:
            found[key] = pe_results
            data = found
        else:
            data = dict(md5=file_md5)
            data[key] = pe_results
        db_insert(data)
        return data
    else:
        pass


def pdfid_scan(file, file_md5):
    this_pdfid = pdfid.PDFiD(file)
    if this_pdfid:
        key, pdfid_results = this_pdfid.scan()
        found = is_hash_in_db(file_md5)
        if found:
            found[key] = pdfid_results
            data = found
        else:
            data = dict(md5=file_md5)
            data[key] = pdfid_results
        db_insert(data)
        return data
    else:
        pass


def run_metascan(file, file_md5):
    # TODO : remove these hardcoded creds
    if config.has_section('Metascan'):
        meta_scan = MetaScan(ip=config.get('Metascan', 'IP'), port=config.get('Metascan', 'Port'))
    else:
        meta_scan = MetaScan(ip='127.0.0.1', port='8008')
    if meta_scan.connected:
        results = meta_scan.scan_file_stream_and_get_results(file)
        if results.status_code != 200:
            print_error("MetaScan can not be reached.")
            return None
        metascan_results = results.json()
        #: Calculate AV Detection Ratio
        detection_ratio = dict(infected=0, count=0)
        for av in metascan_results[u'scan_results'][u'scan_details']:
            metascan_results[u'scan_results'][u'scan_details'][av][u'def_time'] = parser.parse(metascan_results[u'scan_results'][u'scan_details'][av][u'def_time'])
            detection_ratio['count'] += 1
            if metascan_results[u'scan_results'][u'scan_details'][av]['scan_result_i'] == 1:
                detection_ratio['infected'] += 1
        found = is_hash_in_db(file_md5)
        if found:
            found['user_uploads'][-1].setdefault('metascan_results', []).append(metascan_results)
            found['user_uploads'][-1]['detection_ratio']['infected'] += detection_ratio['infected']
            found['user_uploads'][-1]['detection_ratio']['count'] += detection_ratio['count']
            data = found
        else:
            data = dict(md5=file_md5)
            data['user_uploads'][-1].setdefault('metascan_results', []).append(metascan_results)
        db_insert(data)
        return metascan_results
    else:
        return None


@job('low', connection=Redis(), timeout=50)
def run_workers(file_stream):
    # TODO : Automate this so it will scan all registered engines
    # get_file_details(file_stream)
    # pe_info_results = pe_scan(file_stream)
    # print_item("Scanning with Avast.", 1)
    # avast_scan(file_stream)
    # print_item("Scanning with AVG.", 1)
    # avg_scan(file_stream)
    # print_item("Scanning with F-PROT.", 1)
    # f_prot_scan(file_stream)
    # print_item("Scanning with Avira.", 1)
    # avira_scan(file_stream)
    # print_item("Scanning with BitDefender.", 1)
    # bitdefender_scan(file_stream)
    print_item("Scanning with ClamAV.", 1)
    clamav_scan(file_stream)
    # print_item("Scanning with ESET.", 1)
    # eset_scan(file_stream)
    # print_item("Scanning with Kaspersky.", 1)
    # kaspersky_scan(file_stream)
    # print_item("Scanning with Sophos.", 1)
    # sophos_scan(file_stream)
    return True
